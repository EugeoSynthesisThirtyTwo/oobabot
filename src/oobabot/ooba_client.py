# -*- coding: utf-8 -*-
"""
Client for the Ooba API.
Can provide the response by token or by sentence.
"""
import abc
import json
import re
import time
import typing
import requests
import sseclient  # pip install sseclient-py

import aiohttp
import pysbd
import pysbd.utils

from oobabot import fancy_logger
from oobabot import http_client


class MessageSplitter(abc.ABC):
    """
    Split a response into separate messages.
    """

    # anything that can't be in a real response
    END_OF_INPUT = ""

    def __init__(self):
        self.printed_idx = 0
        self.full_response = ""

    def next(self, new_token: str) -> typing.Generator[str, None, None]:
        """
        Collects tokens into a single string, splits into messages
        by the subclass's logic, then yields each message as soon
        as it's found.

        Parameters:
            new_token: str, the next token to add to the string

        Returns:
            Generator[str, None, None], yields each sentence

        Note:
        When there is no longer any input, the caller must pass
        MessageSplitter.END_OF_INPUT to this function.  This
        function will then yield any remaining text, even if it
        doesn't look like a full sentence.
        """

        self.full_response += new_token
        unseen = self.full_response[self.printed_idx :]

        # if we've reached the end of input, yield it all,
        # even if we don't think it's a full sentence.
        if self.END_OF_INPUT == new_token:
            to_print = unseen.strip()
            if to_print:
                yield unseen
            self.printed_idx += len(unseen)
            return

        yield from self.partition(unseen)

    @abc.abstractmethod
    def partition(self, unseen: str) -> typing.Generator[str, None, None]:
        pass


class RegexSplitter(MessageSplitter):
    """
    Split a response into separate messages using a regex.
    """

    def __init__(self, regex: str):
        super().__init__()
        self.pattern = re.compile(regex)

    def partition(self, unseen: str) -> typing.Generator[str, None, None]:
        while True:
            match = self.pattern.match(unseen)
            if not match:
                break
            to_print = match.group(1)
            yield to_print
            self.printed_idx += match.end()
            unseen = self.full_response[self.printed_idx :]


class SentenceSplitter(MessageSplitter):
    """
    Split a response into separate messages using English
    sentence word breaks.
    """

    def __init__(self):
        super().__init__()
        self.segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)

    def partition(self, unseen: str) -> typing.Generator[str, None, None]:
        segments: typing.List[pysbd.utils.TextSpan] = self.segmenter.segment(
            unseen
        )  # type: ignore -- type is determined by char_span=True above

        # any remaining non-sentence things will be in the last element
        # of the list.  Don't print that yet.  At the very worst, we'll
        # print it when the END_OF_INPUT signal is reached.
        for sentence_w_char_spans in segments[:-1]:
            # sentence_w_char_spans is a class with the following fields:
            #  - sent: str, sentence text
            #  - start: start idx of 'sent', relative to original string
            #  - end: end idx of 'sent', relative to original string
            #
            # we want to remove the last '\n' if there is one.
            # we do want to include any other whitespace, though.

            to_print = sentence_w_char_spans.sent  # type: ignore
            if to_print.endswith("\n"):
                to_print = to_print[:-1]

            yield to_print

        # since we've printed all the previous segments,
        # the start of the last segment becomes the starting
        # point for the next round.
        if len(segments) > 0:
            self.printed_idx += segments[-1].start  # type: ignore


class OobaClient:
    """
    Client for the Ooba API.  Can provide the response by token or by sentence.
    """

    SERVICE_NAME = "Oobabooga"

    OOBABOOGA_STREAMING_URI_PATH: str = "/v1/completions"
    OOBABOOGA_HEADERS: dict[str, str] = {"Content-Type": "application/json"}

    def __init__(
        self,
        settings: typing.Dict[str, typing.Any],
    ):
        self.service_name = self.SERVICE_NAME
        self.base_url = settings["base_url"]
        self.total_response_tokens = 0
        self.message_regex = settings["message_regex"]
        self.request_params = settings["request_params"]
        self.log_all_the_things = settings["log_all_the_things"]

        if self.message_regex:
            self.fn_new_splitter = lambda: RegexSplitter(self.message_regex)
        else:
            self.fn_new_splitter = SentenceSplitter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_err):
        pass

    def on_ready(self):
        """
        Called when the client is ready to start.
        Used to log our configuration.
        """
        if self.message_regex:
            fancy_logger.get().debug(
                "Ooba Client: Splitting responses into messages " + "with: %s",
                self.message_regex,
            )
        else:
            fancy_logger.get().debug(
                "Ooba Client: Splitting responses into messages "
                + "by English sentence.",
            )

    def get_stopping_strings(self) -> typing.List[str]:
        """
        Returns a list of strings that indicate the end of a response.
        Taken from the yaml `stopping_strings` within our
        response_params.
        """
        return self.request_params.get("stopping_strings", [])

    async def request_by_message(self, prompt: str) -> typing.AsyncIterator[str]:
        """
        Yields individual messages from the response as it arrives.
        These can be split by a regex or by sentence.
        """
        splitter = self.fn_new_splitter()
        async for new_token in self.request_by_token(prompt):
            for sentence in splitter.next(new_token):
                yield sentence

    async def request_as_string(self, prompt: str) -> str:
        """
        Yields the entire response as a single string.
        """
        return "".join([token async for token in self.request_by_token(prompt)])

    async def request_as_grouped_tokens(
        self,
        prompt: str,
        interval: float = 0.2,
    ) -> typing.AsyncIterator[str]:
        """
        Yields the response as a series of tokens, grouped by time.
        """

        last_response = time.perf_counter()
        tokens = ""
        async for token in self.request_by_token(prompt):
            if token == SentenceSplitter.END_OF_INPUT:
                if tokens:
                    yield tokens
                break
            tokens += token
            now = time.perf_counter()
            if now < (last_response + interval):
                continue
            yield tokens
            tokens = ""
            last_response = time.perf_counter()
    
    def test_connection(self):
        pass

    async def request_by_token(self, prompt: str) -> typing.AsyncIterator[str]:
        """
        Yields each token of the response as it arrives.
        """

        request: dict[
            str, typing.Union[bool, float, int, str, typing.List[typing.Any]]
        ] = {
            "prompt": prompt,
        }
        request.update(self.request_params)
        request["stream"] = True

        stream_response = requests.post(self.base_url + self.OOBABOOGA_STREAMING_URI_PATH, headers=self.OOBABOOGA_HEADERS, json=request, verify=False, stream=True)
        client = sseclient.SSEClient(stream_response)
        
        if self.log_all_the_things:
            try:
                print(f"Sent request:\n{json.dumps(request, indent=1)}")
                print(f"Prompt:\n{str(request['prompt'])}")
            except UnicodeEncodeError:
                print(
                    "Sent request:\n"
                    + f"{json.dumps(request, indent=1).encode('utf-8')}"
                )
                print(f"Prompt:\n{str(request['prompt']).encode('utf-8')}")

        for event in client.events():
            payload = json.loads(event.data)
            text = payload["choices"][0]["text"]
            finish_reason = payload["choices"][0]["finish_reason"]
            # we expect a series of text messages in JSON encoding,
            # like this:
            #
            # text = ""
            # text = "Oh"
            # text = ","
            # text = " okay"
            # text = "."
            
            self.total_response_tokens += 1
            if text != SentenceSplitter.END_OF_INPUT:
                if self.log_all_the_things:
                    try:
                        print(text, end="", flush=True)
                    except UnicodeEncodeError:
                        print(text.encode("utf-8"), end="", flush=True)

                yield text

            if finish_reason is not None:
                # Make sure any unprinted text is flushed.
                if self.log_all_the_things:
                    print("", flush=True)
                yield SentenceSplitter.END_OF_INPUT
                return
