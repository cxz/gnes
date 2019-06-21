#  Tencent is pleased to support the open source community by making GNES available.
#
#  Copyright (C) 2019 THL A29 Limited, a Tencent company. All rights reserved.
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

# pylint: disable=low-comment-ratio

import threading
import uuid
from concurrent import futures
from typing import List

import grpc
import zmq

from .base import BaseService
from ..helper import set_logger
from ..proto import gnes_pb2, gnes_pb2_grpc, send_message, recv_message

__all__ = ['GRPCFrontend']


class BaseServicePool:
    def __init__(self, available_bc: List['BaseService']):
        self.available_bc = available_bc
        self.bc = None

    def __enter__(self):
        self.bc = self.available_bc.pop()
        return self.bc

    def __exit__(self, *args):
        self.available_bc.append(self.bc)


class ZmqContext(object):
    """The zmq context class."""

    def __init__(self, args):
        """Database connection context.

        Args:
            servers: a list of config dicts for connecting to database
            dbapi_name: the name of database engine
        """
        self.args = args

        self.tlocal = threading.local()
        self.tlocal.client = None

    def __enter__(self):
        """Enter the context."""
        client = ZmqClient(self.args)
        self.tlocal.client = client
        return client

    def __exit__(self, exc_type, exc_value, exc_traceback):
        """Exit the context."""
        self.tlocal.client.close()
        self.tlocal.client = None


class ZmqClient:

    def __init__(self, args):
        self.args = args
        self.logger = set_logger(self.__class__.__name__ + ':%s' % self.identity, self.args.verbose)
        self.identity = str(uuid.uuid4())
        self.host_in = args.host_in
        self.host_out = args.host_out
        self.port_in = args.port_in
        self.port_out = args.port_out
        self.context = zmq.Context()
        self.sender = self.context.socket(zmq.PUSH)
        self.sender.connect('tcp://%s:%d' % (self.host_out, self.port_out))
        self.receiver = self.context.socket(zmq.SUB)
        self.receiver.setsockopt(zmq.SUBSCRIBE, self.identity.encode())
        self.receiver.connect('tcp://%s:%d' % (self.host_in, self.port_in))

    def close(self):
        self.sender.close()
        self.receiver.close()
        self.context.term()

    def send_message(self, message: "gnes_pb2.Message", timeout: int = -1):
        send_message(self.sender, message, timeout=timeout)

    def recv_message(self, timeout: int = -1) -> gnes_pb2.Message:
        msg = recv_message(self.receiver, timeout=timeout)
        return msg


class GNESServicer(gnes_pb2_grpc.GnesRPCServicer):

    def __init__(self, args):
        self.args = args
        self.logger = set_logger(self.__class__.__name__, args.verbose)
        self.zmq_context = ZmqContext(args)

    def add_envelope(self, body: 'gnes_pb2.Request', zmq_client: 'ZmqClient'):
        msg = gnes_pb2.Message()
        msg.envelope.client_id = zmq_client.identity if zmq_client.identity else ''
        if body.request_id:
            msg.envelope.request_id = body.request_id
        else:
            msg.envelope.request_id = str(uuid.uuid4())
            self.logger.warning('request_id is missing, filled it with a random uuid!')
        msg.envelope.part_id = 1
        msg.envelope.num_part = 1
        msg.envelope.timeout = 5000
        r = msg.envelope.routes.add()
        r.service = zmq_client.__class__.__name__
        r.timestamp.GetCurrentTime()
        msg.request.CopyFrom(body)
        return msg

    def _Call(self, request, context):
        self.logger.info('received a new request: %s' % request.request_id or 'EMPTY_REQUEST_ID')
        with self.zmq_context as zmq_client:
            msg = self.add_envelope(request, zmq_client)
            zmq_client.send_message(msg, self.args.timeout)
            resp = zmq_client.recv_message(self.args.timeout)
            self.logger.info("received message done!")
            return resp.response

        # with BaseService(self.args, use_event_loop=False) as bs:
        #     msg = self.add_envelope(request, bs)
        #     bs.send_message(msg, self.args.timeout)
        #     resp = bs.recv_message(self.args.timeout)
        #     self.logger.info("received message done!")
        #     return resp.response

    def Train(self, request, context):
        return self._Call(request, context)

    def Index(self, request, context):
        return self._Call(request, context)

    def Search(self, request, context):
        return self._Call(request, context)


class GRPCFrontend:
    def __init__(self, args):
        self.logger = set_logger(self.__class__.__name__, args.verbose)
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=args.max_concurrency))
        self.logger.info('start a grpc server with %d workers' % args.max_concurrency)
        gnes_pb2_grpc.add_GnesRPCServicer_to_server(GNESServicer(args), self.server)

        # Start GRPC Server
        self.bind_address = '{0}:{1}'.format(args.grpc_host, args.grpc_port)
        self.server.add_insecure_port(self.bind_address)

    def __enter__(self):
        self.server.start()
        self.logger.info('grpc service is listening at: %s' % self.bind_address)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass