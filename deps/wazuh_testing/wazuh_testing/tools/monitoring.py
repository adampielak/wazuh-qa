# Copyright (C) 2015-2020, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is free software; you can redistribute it and/or modify it under the terms of GPLv2

# Unix only modules

try:
    import grp
    import pwd
except ModuleNotFoundError:
    pass

import os
import queue
import re
import socket
import socketserver
import sys
import threading
import time
from collections import defaultdict
from copy import copy
from multiprocessing import Process, Manager
from struct import pack, unpack

import yaml
from lockfile import FileLock

from wazuh_testing import logger
from wazuh_testing.tools.file import truncate_file
from wazuh_testing.tools.system import HostManager
from wazuh_testing.tools.time import Timer


def wazuh_unpack(data, format_: str = "<I"):
    """Unpack data with a given header. Using Wazuh header by default.

    Parameters
    ----------
    data : bytes
        Binary data to unpack
    format_ : str, optional
        Format used to unpack data. Default "<I"

    Returns
    -------
    int
        Unpacked value
    """
    return unpack(format_, data)[0]


def wazuh_pack(data, format_: str = "<I"):
    """Pack data with a given header. Using Wazuh header by default.

    Parameters
    ----------
    data : int
        Int number to pack
    format_ : str, optional
        Format used to pack data. Default "<I"

    Returns
    -------
    bytes
        Packed value
    """
    return pack(format_, data)


def wait_for_condition(condition_checker, args=None, kwargs=None, timeout=-1):
    """Wait for a given condition to check.

    Parameters
    ----------
    condition_checker : callable
        Function that checks a condition.
    args :  list, optional
        List of positional arguments. Default `None`
    kwargs : dict, optional
        Dict of non positional arguments. Default `None`
    timeout : int, optional
        Time to wait. Default `-1`

    Raises
    ------
    TimeoutError
        If `timeout` is not -1 and there have been more iterations that the max allowed.
    """
    args = [] if args is None else args
    kwargs = {} if kwargs is None else kwargs
    time_step = 0.5
    max_iterations = timeout / time_step
    iterations = 0
    while not condition_checker(*args, **kwargs):
        if timeout != -1 and iterations > max_iterations:
            raise TimeoutError()
        iterations += 1
        time.sleep(time_step)


def _callback_default(line):
    print(line)
    return None


class FileTailer:

    def __init__(self, file_path, encoding=None, time_step=0.5):
        self.file_path = file_path
        self._position = 0
        self.time_step = time_step
        self._queue = Queue()
        self.event = threading.Event()
        self.thread = None
        if sys.platform == 'win32':
            self.encoding = None if encoding is None else encoding
        elif encoding is None:
            self.encoding = 'utf-8'

    def __copy__(self):
        new_tailer = FileTailer(self.file_path)
        for attr, value in vars(self).items():
            if attr == 'file_path':
                continue
            elif attr != '_queue':
                setattr(new_tailer, attr, value)
            else:
                new_queue = Queue()
                new_queue.queue = copy(getattr(self, attr).queue)
                setattr(new_tailer, attr, new_queue)
        return new_tailer

    @property
    def queue(self):
        return self._queue

    def add_item(self, item):
        self._queue.put(item)

    def start(self):
        self.run()

    def run(self):
        self.event = threading.Event()
        self.thread = threading.Thread(target=self._tail_forever)
        self.thread.start()

    def shutdown(self):
        self.event.set()
        self.thread.join()

    def _tail_forever(self):
        """Wait for new lines to be appended to the file."""
        with open(self.file_path, encoding=self.encoding, errors='backslashreplace') as f:
            f.seek(self._position)
            while not self.event.is_set():
                line = f.readline()
                if not line:
                    f.seek(self._position)
                    time.sleep(self.time_step)
                else:
                    self.add_item(line)
                self._position = f.tell()


class FileMonitor:

    def __init__(self, file_path, time_step=0.5):
        self.tailer = FileTailer(file_path, time_step=time_step)
        self._result = None
        self._time_step = time_step

    def start(self, timeout=-1, callback=_callback_default, accum_results=1, update_position=True, timeout_extra=0,
              error_message='', encoding=None):
        """Start the file monitoring until the stop method is called."""
        try:
            tailer = self.tailer if update_position else copy(self.tailer)

            if encoding is not None:
                tailer.encoding = encoding
            tailer.start()

            monitor = QueueMonitor(tailer.queue, time_step=self._time_step)
            self._result = monitor.start(timeout=timeout, callback=callback, accum_results=accum_results,
                                         update_position=update_position, timeout_extra=timeout_extra,
                                         error_message=error_message).result()
        finally:
            tailer.shutdown()

        return self

    def result(self):
        return self._result


class SocketController:

    def __init__(self, address, family='AF_UNIX', connection_protocol='TCP', timeout=30):
        """Create a new unix socket or connect to a existing one.

        Parameters
        ----------
        address : str or Tuple(str, int)
            Address of the socket, the format of the address depends on the type. A regular file path for AF_UNIX or a
            Tuple(HOST, PORT) for AF_INET
        family : str
            Family type of socket to connect to, AF_UNIX for unix sockets or AF_INET for port sockets.
        connection_protocol : str
            Flag that indicates if the connection is TCP (SOCK_STREAM) or UDP (SOCK_DGRAM).
        timeout : int, optional
            Socket's timeout, 0 for non-blocking mode.

        Raises
        ------
        Exception
            If the socket connection failed.
        """
        self.address = address

        # Set socket family
        if family == 'AF_UNIX':
            self.family = socket.AF_UNIX
        elif family == 'AF_INET':
            self.family = socket.AF_INET
        else:
            raise TypeError(f'Invalid family type detected: {family}. Valid ones are AF_UNIX or AF_INET')

        # Set socket protocol
        if connection_protocol.lower() == 'tcp':
            self.protocol = socket.SOCK_STREAM
        elif connection_protocol.lower() == 'udp':
            self.protocol = socket.SOCK_DGRAM
        else:
            raise TypeError(f'Invalid connection protocol detected: {connection_protocol.lower()}. '
                            f'Valid ones are TCP or UDP')

        # Create socket object
        self.sock = socket.socket(family=self.family, type=self.protocol)

        # Connect only if protocol is TCP
        if self.protocol == socket.SOCK_STREAM:
            try:
                self.sock.settimeout(timeout)
                self.sock.connect(self.address)
            except socket.timeout as e:
                raise TimeoutError(f'Could not connect to socket {self.address} of family {self.family}')

    def close(self):
        """Close the socket gracefully."""
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()

    def send(self, message, size=False):
        """Send a message to the socket.

        Parameters
        ----------
        message : str or bytes
            Message to be sent.
        size : bool, optional
            Flag that indicates if the header of the message includes the size of the message.
            (For example, Analysis doesn't need the size, wazuh-db does). Default `False`

        Returns
        -------
        int
            Size of the sent message
        """
        msg_bytes = message.encode() if isinstance(message, str) else message
        try:
            msg_bytes = wazuh_pack(len(msg_bytes)) + msg_bytes if size is True else msg_bytes
            if self.protocol == socket.SOCK_STREAM:  # TCP
                output = self.sock.sendall(msg_bytes)
            else:  # UDP
                output = self.sock.sendto(msg_bytes, self.address)
        except OSError as e:
            raise e

        return output

    def receive(self, size=False):
        """Receive a message from the socket.

        Parameters
        ----------
        size : bool, optional
            Flag that indicates if the header of the message includes the size of the message.
            (For example, Analysis doesn't need the size, wazuh-db does). Default `False`

        Returns
        -------
        bytes
            Socket message.
        """
        if size is True:
            size = wazuh_unpack(self.sock.recv(4, socket.MSG_WAITALL))
            output = self.sock.recv(size, socket.MSG_WAITALL)
        else:
            output = self.sock.recv(4096)
            if len(output) == 4096:
                while 1:
                    try:  # error means no more data
                        output += self.sock.recv(4096, socket.MSG_DONTWAIT)
                    except:
                        break

        return output

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class QueueMonitor:
    def __init__(self, queue_item, time_step=0.5):
        """Create a new instance to monitor any given queue.

        Parameters
        ----------
        queue_item : Queue
            Queue to monitor.
        time_step : float, optional
            Fraction of time to wait in every get. Default `0.5`
        """
        self._queue = queue_item
        self._continue = False
        self._abort = False
        self._result = None
        self._time_step = time_step

    def get_results(self, callback=_callback_default, accum_results=1, timeout=-1, update_position=True,
                    timeout_extra=0):
        """Get as many matched results as `accum_results`.

        Parameters
        ----------
        callback : callable, optional
            Callback function to filter results.
        accum_results : int, optional
            Number of results to get. Default `1`
        timeout : int, optional
            Maximum timeout. Default `-1`
        update_position : bool, optional
            True if we pop items from the queue once they are read. False otherwise. Default `True`
        timeout_extra : int, optional
            Grace period to fetch more events than specified in `accum_results`. Default: 0.

        Returns
        -------
        list of any or any
            It can return either a list of any type or simply any type. If `accum_results > 1`, it will be a list.
        """
        result_list = []
        timer = 0.0
        time_wait = 0.1
        position = 0
        extra_timer_is_running = False
        extra_timer = 0.0
        while len(result_list) != accum_results or extra_timer_is_running:
            if timer >= timeout and not extra_timer_is_running:
                self.abort()
                break
            if extra_timer >= timeout_extra > 0:
                self.stop()
                break
            tic = time.time()
            try:
                if update_position:
                    item = callback(self._queue.get(block=True, timeout=self._time_step))
                else:
                    item = callback(self._queue.peek(position=position, block=True, timeout=self._time_step))
                    position += 1
                if item is not None:
                    result_list.append(item)
                    if len(result_list) == accum_results and timeout_extra > 0 and not extra_timer_is_running:
                        extra_timer_is_running = True
            except queue.Empty:
                pass
            finally:
                time_count = time.time() - tic
                timer += time_count
                if extra_timer_is_running:
                    extra_timer += time_count

        if len(result_list) == 1:
            return result_list[0]
        else:
            return result_list

    def start(self, timeout=-1, callback=_callback_default, accum_results=1, update_position=True, timeout_extra=0,
              error_message=''):
        """Start the queue monitoring until the stop method is called."""
        if not self._continue:
            self._continue = True
            self._abort = False

            while self._continue:
                if self._abort:
                    self.stop()
                    if error_message:
                        logger.error(error_message)
                        logger.error(f"Results accumulated: "
                                     f"{len(self._result) if isinstance(self._result, list) else 0}")
                        logger.error(f"Results expected: {accum_results}")
                    raise TimeoutError()
                result = self.get_results(callback=callback, accum_results=accum_results, timeout=timeout,
                                          update_position=update_position, timeout_extra=timeout_extra)
                if result and not self._abort:
                    self._result = result
                    if self._result:
                        self.stop()

        return self

    def stop(self):
        """Stop the queue monitoring. It can be restart calling the start method."""
        self._continue = False
        return self

    def abort(self):
        """Abort because of timeout."""
        self._abort = True
        return self

    def result(self):
        """Return the current result."""
        return self._result

    def get_queue(self):
        """Return the monitored queue."""
        return self._queue


class Queue(queue.Queue):
    def peek(self, *args, position=0, **kwargs):
        """Peek any given position without modifying the queue status.

        The difference between `peek` and `get` is `peek` pops the item and `get` does not.

        Parameters
        ----------
        position : int, optional
            Element of the queue to return. Default `0`

        Returns
        -------
        any
            Any item in the given position.
        """
        aux_queue = queue.Queue()
        aux_queue.queue = copy(self.queue)
        for _ in range(position):
            aux_queue.get(*args, **kwargs)
        return aux_queue.get(*args, **kwargs)


class StreamServerPort(socketserver.ThreadingTCPServer):
    pass


class DatagramServerPort(socketserver.ThreadingUDPServer):
    pass


if hasattr(socketserver, 'ThreadingUnixStreamServer'):

    class StreamServerUnix(socketserver.ThreadingUnixStreamServer):

        def shutdown_request(self, request):
            pass

    class DatagramServerUnix(socketserver.ThreadingUnixDatagramServer):

        def shutdown_request(self, request):
            pass


    class StreamHandler(socketserver.BaseRequestHandler):

        def unix_forward(self, data):
            """Default TCP unix socket forwarder for MITM servers."""
            # Create a socket context
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as forwarded_sock:
                # Connect to server and send data
                forwarded_sock.connect(self.server.mitm.forwarded_socket_path)
                forwarded_sock.sendall(wazuh_pack(len(data)) + data)

                # Receive data from the server and shut down
                size = wazuh_unpack(self.recvall_size(forwarded_sock, 4, socket.MSG_WAITALL))
                response = self.recvall_size(forwarded_sock, size, socket.MSG_WAITALL)

                return response

        def recvall_size(self, sock: socket.socket, size: int, mask: int):
            """Recvall with known size of the message."""
            buffer = bytearray()
            while len(buffer) < size:
                try:
                    data = sock.recv(size - len(buffer), mask)
                    if not data:
                        break
                    buffer.extend(data)
                except socket.timeout:
                    if self.server.mitm.event.is_set():
                        break
            return bytes(buffer)

        def recvall(self, chunk_size: int = 4096):
            """Recvall without known size of the message."""
            received = self.request.recv(chunk_size)
            if len(received) == chunk_size:
                while 1:
                    try:  # error means no more data
                        received += self.request.recv(chunk_size, socket.MSG_DONTWAIT)
                    except:
                        break
            return received

        def default_wazuh_handler(self):
            """Default wazuh daemons TCP handler method for MITM server."""
            self.request.settimeout(1)
            while not self.server.mitm.event.is_set():
                header = self.recvall_size(self.request, 4, socket.MSG_WAITALL)
                if not header:
                    break
                size = wazuh_unpack(header)
                data = self.recvall_size(self.request, size, socket.MSG_WAITALL)
                if not data:
                    break

                response = self.unix_forward(data)

                self.server.mitm.put_queue((data.rstrip(b'\x00'), response.rstrip(b'\x00')))

                self.request.sendall(wazuh_pack(len(response)) + response)

        def handle(self):
            """Overriden handle method for TCP MITM server."""
            if self.server.mitm.handler_func is None:
                self.default_wazuh_handler()
            else:
                while not self.server.mitm.event.is_set():
                    received = self.recvall()
                    response = self.server.mitm.handler_func(received)
                    self.server.mitm.put_queue((received, response))
                    self.request.sendall(response)

    class DatagramHandler(socketserver.BaseRequestHandler):

        def unix_forward(self, data):
            """Default UDP unix socket forwarder for MITM servers."""
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as forwarded_sock:
                forwarded_sock.sendto(data, self.server.mitm.forwarded_socket_path)

        def default_wazuh_handler(self):
            """Default wazuh daemons UDP handler method for MITM server."""
            data = self.request[0]
            self.unix_forward(data)
            self.server.mitm.put_queue(data.rstrip(b'\x00'))

        def handle(self):
            """Overriden handle method for UDP MITM server."""
            if self.server.mitm.handler_func is None:
                self.default_wazuh_handler()
            else:
                data = self.request[0]
                self.server.mitm.handler_func(data)
                self.server.mitm.put_queue(data)

    class ManInTheMiddle:

        def __init__(self, address, family='AF_UNIX', connection_protocol='TCP', func: callable = None):
            """Create a MITM server for the socket `socket_address`.

            Parameters
            ----------
            address : str or Tuple(str, int)
                Address of the socket, the format of the address depends on the type. A regular file path for AF_UNIX or
                a Tuple(HOST, PORT) for AF_INET
            family : str
                Family type of socket to connect to, AF_UNIX for unix sockets or AF_INET for port sockets.
                Default `'AF_UNIX'`
            connection_protocol : str
                It can be either 'TCP' or 'UDP'. Default `'TCP'`
            func : callable
                Function to be applied to every received data before sending it.
            """
            if isinstance(address, str) or (isinstance(address, tuple) and len(address) == 2
                                            and isinstance(address[0], str) and isinstance(address[1], int)):
                self.listener_socket_address = address
            else:
                raise TypeError(f"Invalid address type: {type(address)}. Valid types are str or Tuple(str, int)")

            if connection_protocol.lower() == 'tcp' or connection_protocol.lower() == 'udp':
                self.mode = connection_protocol.lower()
            else:
                raise TypeError(f'Invalid connection protocol detected: {connection_protocol.lower()}. '
                                f'Valid ones are TCP or UDP')

            if family in ('AF_UNIX', 'AF_INET'):
                self.family = family
            else:
                raise TypeError('Invalid family type detected. Valid ones are AF_UNIX or AF_INET')

            self.forwarded_socket_path = None

            class_tree = {
                'listener': {
                    'tcp': {
                        'AF_UNIX': StreamServerUnix,
                        'AF_INET': StreamServerPort
                    },
                    'udp': {
                        'AF_UNIX': DatagramServerUnix,
                        'AF_INET': DatagramServerPort
                    }
                },
                'handler': {
                    'tcp': StreamHandler,
                    'udp': DatagramHandler
                }
            }

            self.listener_class = class_tree['listener'][self.mode][self.family]
            self.handler_class = class_tree['handler'][self.mode]
            self.handler_func = func
            self.listener = None
            self.thread = None
            self.event = threading.Event()
            self._queue = Queue()

        def run(self, *args):
            """Run a MITM server."""
            # Rename socket if it is a file (AF_UNIX)
            if isinstance(self.listener_socket_address, str):
                self.forwarded_socket_path = f'{self.listener_socket_address}.original'
                os.rename(self.listener_socket_address, self.forwarded_socket_path)

            self.listener_class.allow_reuse_address = True
            self.listener = self.listener_class(self.listener_socket_address, self.handler_class)
            self.listener.mitm = self

            # Give proper permissions to socket
            if isinstance(self.listener_socket_address, str):
                uid = pwd.getpwnam('ossec').pw_uid
                gid = grp.getgrnam('ossec').gr_gid
                os.chown(self.listener_socket_address, uid, gid)
                os.chmod(self.listener_socket_address, 0o660)

            self.thread = threading.Thread(target=self.listener.serve_forever)
            self.thread.start()

        def start(self):
            self.run()

        def shutdown(self):
            """Gracefully shutdown a MITM server."""
            self.listener.shutdown()
            self.listener.socket.close()
            self.event.set()
            # Remove created unix socket and restore original
            if isinstance(self.listener_socket_address, str):
                os.remove(self.listener_socket_address)
                os.rename(self.forwarded_socket_path, self.listener_socket_address)

        @property
        def queue(self):
            return self._queue

        def put_queue(self, item):
            self._queue.put(item)


def new_process(fn):
    """Wrapper for enable multiprocessing inside a class

    Parameters
    ----------
    fn : callable
        Function to be executed in a new thread

    Returns
    -------
    wrapper
    """

    def wrapper(*args, **kwargs):
        thread = Process(target=fn, args=args, kwargs=kwargs)
        thread.start()
        return thread

    return wrapper


def callback_generator(regex):
    def new_callback(line):
        match = re.match(regex, line)
        if match:
            return line

    return new_callback


class HostMonitor:
    """This class has the capability to monitor remote host. This monitoring consists of reading the specified files to
    check that the expected message arrives to them.

    If the goals are achieved, no exceptions will be raised and therefore the test will end properly and without
    failures.

    In contrast, if one or more of the goals is not covered, a timeout exception will be raised with a generic or a
    custom error message.
    """

    def __init__(self, inventory_path, messages_path, tmp_path, time_step=0.5):
        """Create a new instance to monitor any given file in any specified host.

        Parameters
        ----------
        inventory_path : str
            Path to the hosts's inventory file.
        messages_path : str
            Path to the file where the callbacks, paths and hosts to be monitored are specified.
        tmp_path : str
            Path to the temporal files.
        time_step : float, optional
            Fraction of time to wait in every get. Default `0.5`.
        """
        self.host_manager = HostManager(inventory_path=inventory_path)
        self._queue = Manager().Queue()
        self._result = defaultdict(list)
        self._time_step = time_step
        self._file_monitors = list()
        self._monitored_files = set()
        self._file_content_collectors = list()
        self._tmp_path = tmp_path
        try:
            os.mkdir(self._tmp_path)
        except OSError:
            pass
        with open(messages_path, 'r') as f:
            self.test_cases = yaml.safe_load(f)

    def run(self):
        """This method creates and destroy the needed processes for the messages founded in messages_path.
        It creates one file composer (process) for every file to be monitored in every host."""
        for host, payload in self.test_cases.items():
            self._monitored_files.update({case['path'] for case in payload})
            if len(self._monitored_files) == 0:
                raise AttributeError('There is no path to monitor. Exiting...')
            for path in self._monitored_files:
                output_path = f'{host}_{path.split("/")[-1]}.tmp'
                self._file_content_collectors.append(self.file_composer(host=host, path=path, output_path=output_path))
                logger.debug(f'Add new file composer process for {host} and path: {path}')
                self._file_monitors.append(self._start(host=host, payload=payload, path=output_path))
                logger.debug(f'Add new file monitor process for {host} and path: {path}')

        while True:
            if not any([handler.is_alive() for handler in self._file_monitors]):
                for handler in self._file_monitors:
                    handler.join()
                for file_collector in self._file_content_collectors:
                    file_collector.terminate()
                    file_collector.join()
                self.clean_tmp_files()
                break
            time.sleep(self._time_step)
        self.check_result()

    @new_process
    def file_composer(self, host, path, output_path):
        """Collects the file content of the specified path in the desired host and append it to the output_path file.
        Simulates the behavior of tail -f and redirect the output to output_path.

        Parameters
        ----------
        host : str
            Hostname.
        path : str
            Host file path to be collect.
        output_path : str
            Output path of the content collected from the remote host path.
        """
        try:
            truncate_file(os.path.join(self._tmp_path, output_path))
        except FileNotFoundError:
            pass
        logger.debug(f'Starting file composer for {host} and path: {path}. '
                     f'Composite file in {os.path.join(self._tmp_path, output_path)}')
        tmp_file = os.path.join(self._tmp_path, output_path)
        while True:
            with FileLock(tmp_file):
                with open(tmp_file, "r+") as file:
                    content = self.host_manager.get_file_content(host, path).split('\n')
                    file_content = file.read().split('\n')
                    for new_line in content:
                        if new_line == '':
                            continue
                        if new_line not in file_content:
                            file.write(f'{new_line}\n')
                time.sleep(self._time_step)

    @new_process
    def _start(self, host, payload, path, encoding=None, error_messages_per_host=None):
        """Start the file monitoring until the QueueMonitor returns an string or TimeoutError.

        Parameters
        ----------
        host : str
            Hostname
        payload : list of dict
            Contains the message to be found and the timeout for it.
        path : str
            Path where it must search for the message.
        encoding : str
            Encoding of the file.
        error_messages_per_host : dict
            Dictionary with hostnames as keys and desired error messages as values

        Returns
        -------
        instance of HostMonitor
        """
        tailer = FileTailer(os.path.join(self._tmp_path, path), time_step=self._time_step)
        try:
            if encoding is not None:
                tailer.encoding = encoding
            tailer.start()
            for case in payload:
                logger.debug(f'Starting QueueMonitor for {host} and message: {case["regex"]}')
                monitor = QueueMonitor(tailer.queue, time_step=self._time_step)
                try:
                    self._queue.put({host: monitor.start(timeout=case['timeout'],
                                                         callback=callback_generator(case['regex'])
                                                         ).result().strip('\n')})
                except TimeoutError:
                    try:
                        self._queue.put({host: error_messages_per_host[host]})
                    except (KeyError, TypeError):
                        self._queue.put({
                            host: TimeoutError(f'Did not found the expected callback in {host}: {case["regex"]}')})
                logger.debug(f'Finishing QueueMonitor for {host} and message: {case["regex"]}')
        finally:
            tailer.shutdown()

        return self

    def result(self):
        """Get the result of HostMonitor

        Returns
        -------
        dict
            Dict that contains the host as the key and a list of messages as the values
        """
        return self._result

    def check_result(self):
        """Check if a TimeoutError occurred."""
        logger.debug(f'Checking results...')
        while not self._queue.empty():
            result = self._queue.get(block=True)
            for host, msg in result.items():
                if isinstance(msg, TimeoutError):
                    raise msg
                logger.debug(f'Received from {host} the expected message: {msg}')
                self._result[host].append(msg)

    def clean_tmp_files(self):
        """Remove tmp files."""
        logger.debug(f'Cleaning temporal files...')
        for file in os.listdir(self._tmp_path):
            os.remove(os.path.join(self._tmp_path, file))
