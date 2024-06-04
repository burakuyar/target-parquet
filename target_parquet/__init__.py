#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gc
import http.client
import os
import sys
import threading
import time
import urllib
from datetime import datetime
from enum import Enum
from io import TextIOWrapper
from multiprocessing import get_context
from typing import TYPE_CHECKING

import pkg_resources
import psutil
import pyarrow as pa
import simplejson as json
import singer
from jsonschema.validators import Draft4Validator
from pyarrow.parquet import ParquetWriter

from .helpers import flatten, flatten_schema

if TYPE_CHECKING:
    from multiprocessing import Queue

_all__ = ["main"]

LOGGER = singer.get_logger()
LOGGER.setLevel(os.getenv("LOGGER_LEVEL", "INFO"))


def create_dataframe(list_dict):
    fields = set()
    for d in list_dict:
        fields = fields.union(d.keys())
    dataframe = pa.table({f: [row.get(f) for row in list_dict] for f in fields})
    return dataframe


class MessageType(Enum):
    RECORD = 1
    STATE = 2
    SCHEMA = 3
    EOF = 4


def emit_state(state):
    if state is not None:
        line = json.dumps(state)
        LOGGER.debug("Emitting state {}".format(line))
        sys.stdout.write("{}\n".format(line))
        sys.stdout.flush()


class MemoryReporter(threading.Thread):
    """Logs memory usage every 30 seconds"""

    def __init__(self):
        self.process = psutil.Process()
        super().__init__(name="memory_reporter", daemon=True)

    def run(self):
        while True:
            LOGGER.debug(
                "Virtual memory usage: %.2f%% of total: %s",
                self.process.memory_percent(),
                self.process.memory_info(),
            )
            time.sleep(30.0)


def persist_messages(
    messages,
    destination_path,
    parquet_version,
    compression_method=None,
    streams_in_separate_folder=False,
    file_size=-1,
):
    # Multiprocessing context
    if sys.platform == "darwin" or sys.platform == 'linux':
        ctx = get_context("fork")
    else:
        ctx = get_context("spawn")

    ## Static information shared among processes
    schemas = {}
    key_properties = {}
    validators = {}

    compression_extension = ""
    if compression_method:
        # The target is prepared to accept all the compression methods provided by the pandas module, with the mapping below,
        extension_mapping = {
            "SNAPPY": ".snappy",
            "GZIP": ".gz",
            "BROTLI": ".br",
            "ZSTD": ".zstd",
            "LZ4": ".lz4",
        }
        compression_extension = extension_mapping.get(compression_method.upper())
        if compression_extension is None:
            LOGGER.warning("unsuported compression method.")
            compression_extension = ""
            compression_method = None
    filename_separator = "-"
    if streams_in_separate_folder:
        LOGGER.info("writing streams in separate folders")
        filename_separator = os.path.sep
    if not os.path.exists(destination_path):
        os.makedirs(destination_path)
    ## End of Static information shared among processes

    # Object that signals shutdown
    _break_object = object()

    def producer(message_buffer: TextIOWrapper, w_queue: Queue):
        state = None
        try:
            for message in message_buffer:
                LOGGER.debug(f"target-parquet got message: {message}")
                try:
                    message = singer.parse_message(message).asdict()
                except json.decoder.JSONDecodeError:
                    raise Exception("Unable to parse:\n{}".format(message))

                message_type = message["type"]
                if message_type == "RECORD":
                    if message["stream"] not in schemas:
                        raise ValueError(
                            "A record for stream {} was encountered before a corresponding schema".format(
                                message["stream"]
                            )
                        )
                    stream_name = message["stream"]
                    validators[message["stream"]].validate(message["record"])
                    flattened_record = flatten(message["record"])
                    # Once the record is flattenned, it is added to the final record list, which will be stored in the parquet file.
                    w_queue.put((MessageType.RECORD, stream_name, flattened_record))
                    state = None
                elif message_type == "STATE":
                    LOGGER.debug("Setting state to {}".format(message["value"]))
                    state = message["value"]
                elif message_type == "SCHEMA":
                    stream = message["stream"]
                    validators[stream] = Draft4Validator(message["schema"])
                    schemas[stream] = flatten_schema(message["schema"]["properties"])
                    LOGGER.debug(f"Schema: {schemas[stream]}")
                    key_properties[stream] = message["key_properties"]
                    w_queue.put((MessageType.SCHEMA, stream, schemas[stream]))
                else:
                    LOGGER.warning(
                        "Unknown message type {} in message {}".format(
                            message["type"], message
                        )
                    )
            w_queue.put((MessageType.EOF, _break_object, None))
            return state
        except Exception as Err:
            w_queue.put((MessageType.EOF, _break_object, None))
            raise Err

    def write_file(current_stream_name, record):
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S-%f")
        LOGGER.debug(f"Writing files from {current_stream_name} stream")
        dataframe = create_dataframe(record)
        if streams_in_separate_folder and not os.path.exists(
            os.path.join(destination_path, current_stream_name)
        ):
            os.makedirs(os.path.join(destination_path, current_stream_name))
        filename = (
            current_stream_name
            + filename_separator
            + timestamp
            + compression_extension
            + ".parquet"
        )
        filepath = os.path.expanduser(os.path.join(destination_path, filename))
        LOGGER.info(f"target-parquet filepath is: {filepath}")
        ParquetWriter(
            filepath, dataframe.schema, compression=compression_method, version=parquet_version
        ).write_table(dataframe)

        ## explicit memory management. This can be usefull when working on very large data groups
        del dataframe
        return filepath

    def consumer(receiver):
        files_created = []
        current_stream_name = None
        # records is a list of dictionary of lists of dictionaries that will contain the records that are retrieved from the tap
        records = {}
        schemas = {}

        while True:
            (message_type, stream_name, record) = receiver.get()  # q.get()
            if message_type == MessageType.RECORD:
                if (stream_name != current_stream_name) and (
                    current_stream_name != None
                ):
                    files_created.append(
                        write_file(
                            current_stream_name, records.pop(current_stream_name)
                        )
                    )
                    ## explicit memory management. This can be usefull when working on very large data groups
                    gc.collect()
                current_stream_name = stream_name
                if type(records.get(stream_name)) != list:
                    records[stream_name] = [record]
                else:
                    records[stream_name].append(record)
                    if (file_size > 0) and (not len(records[stream_name]) % file_size):
                        files_created.append(
                            write_file(
                                current_stream_name, records.pop(current_stream_name)
                            )
                        )
                        gc.collect()
            elif message_type == MessageType.SCHEMA:
                schemas[stream_name] = record
            elif message_type == MessageType.EOF:
                files_created.append(
                    write_file(current_stream_name, records.pop(current_stream_name))
                )
                LOGGER.info(f"Wrote {len(files_created)} files")
                LOGGER.debug(f"Wrote {files_created} files")
                break

    q = ctx.Queue()
    t2 = ctx.Process(
        target=consumer,
        args=(q,),
    )
    t2.start()
    state = producer(messages, q)
    t2.join()
    return state


def send_usage_stats():
    try:
        version = pkg_resources.get_distribution("target-parquet").version
        conn = http.client.HTTPConnection("collector.singer.io", timeout=10)
        conn.connect()
        params = {
            "e": "se",
            "aid": "singer",
            "se_ca": "target-parquet",
            "se_ac": "open",
            "se_la": version,
        }
        conn.request("GET", "/i?" + urllib.parse.urlencode(params))
        conn.getresponse()
        conn.close()
    except:
        LOGGER.debug("Collection request failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", help="Config file")

    args = parser.parse_args()
    if args.config:
        with open(args.config) as input_json:
            config = json.load(input_json)
    else:
        config = {}
        level = config.get("logging_level", None)
        if level:
            LOGGER.setLevel(level)
    if not config.get("disable_collection", False):
        LOGGER.info(
            "Sending version information to singer.io. "
            + "To disable sending anonymous usage data, set "
            + 'the config parameter "disable_collection" to true'
        )
        threading.Thread(target=send_usage_stats).start()
    # The target expects that the tap generates UTF-8 encoded text.
    input_messages = TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    if LOGGER.level == 0:
        MemoryReporter().start()
    state = persist_messages(
        input_messages,
        destination_path=config.get("destination_path", "."),
        compression_method=config.get("compression_method", None),
        parquet_version=config.get("parquet_version", "1.0"),
        streams_in_separate_folder=config.get("streams_in_separate_folder", False),
        file_size=int(config.get("file_size", -1)),
    )

    emit_state(state)
    LOGGER.debug("Exiting normally")


if __name__ == "__main__":
    main()
