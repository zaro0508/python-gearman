import logging
import select as select_lib

import gearman.util
from gearman.connection import GearmanConnection
from gearman.constants import _DEBUG_MODE_
from gearman.errors import ConnectionError, ServerUnavailable
from gearman.job import GearmanJob, GearmanJobRequest
from gearman import compat

gearman_logger = logging.getLogger(__name__)

class DataEncoder(object):
    @classmethod
    def encode(cls, encodable_object):
        raise NotImplementedError

    @classmethod
    def decode(cls, decodable_string):
        raise NotImplementedError

class NoopEncoder(DataEncoder):
    """Provide common object dumps for all communications over gearman"""
    @classmethod
    def _enforce_byte_string(cls, given_object):
        if type(given_object) != str:
            raise TypeError("Expecting byte string, got %r" % type(given_object))

    @classmethod
    def encode(cls, encodable_object):
        cls._enforce_byte_string(encodable_object)
        return encodable_object

    @classmethod
    def decode(cls, decodable_string):
        cls._enforce_byte_string(decodable_string)
        return decodable_string

class GearmanConnectionManager(object):
    """Abstract base class for any Gearman-type client that needs to connect/listen to multiple connections

    Mananges and polls a group of gearman connections
    Forwards all communication between a connection and a command handler
    The state of a connection is represented within the command handler

    Automatically encodes all 'data' fields as specified in protocol.py
    """
    command_handler_class = None
    connection_class = GearmanConnection

    job_class = GearmanJob
    job_request_class = GearmanJobRequest

    data_encoder = NoopEncoder

    def __init__(self, host_list=None):
        assert self.command_handler_class is not None, 'GearmanClientBase did not receive a command handler class'

        self.fd_to_connection_map = {}
        self.connection_list = []

        self.handler_initial_state = {}

        host_list = host_list or []
        for hostport_tuple in host_list:
            self.add_connection(hostport_tuple)

    def shutdown(self):
        # Shutdown all our connections one by one
        for gearman_connection in self.connection_list:
            try:
                gearman_connection.close()
            except ConnectionError:
                pass

    ###################################
    # Connection management functions #
    ###################################

    def add_connection(self, hostport_tuple):
        """Add a new connection to this connection manager"""
        gearman_host, gearman_port = gearman.util.disambiguate_server_parameter(hostport_tuple)

        current_connection = self.connection_class(host=gearman_host, port=gearman_port)

        # Establish a connection immediately - check for socket exceptions like: "host not found"
        client_connection = self.establish_connection(current_connection)

        return client_connection

    def establish_connection(self, current_connection):
        # Asynchronously establish a connection
        current_connection.connect()

        connection_fd = current_connection.fileno()
        self.fd_to_connection_map[connection_fd] = current_connection

        self.connection_list.append(current_connection)

        # Initiate a new command handler every time we start a new connection
        current_handler = self.command_handler_class(connection_manager=self)

        current_handler.initial_state(**self.handler_initial_state)
        return current_connection

    def poll_connections_once(self, submitted_connections, timeout=None):
        """Does a single robust select, catching socket errors"""
        select_connections = set(submitted_connections)

        rd_connections = set()
        wr_connections = set()
        ex_connections = set()

        if timeout is not None and timeout < 0.0:
            return rd_connections, wr_connections, ex_connections

        successful_select = False
        while not successful_select and select_connections:
            select_connections -= ex_connections
            check_rd_connections = [current_connection for current_connection in select_connections if current_connection.readable()]
            check_wr_connections = [current_connection for current_connection in select_connections if current_connection.writable()]

            try:
                rd_list, wr_list, ex_list = gearman.util.select(check_rd_connections, check_wr_connections, select_connections, timeout=timeout)
                rd_connections |= set(rd_list)
                wr_connections |= set(wr_list)
                ex_connections |= set(ex_list)

                successful_select = True
            except (select_lib.error, ConnectionError):
                # On any exception, we're going to assume we ran into a socket exception
                # We'll need to fish for bad connections as suggested at
                #
                # http://docs.python.org/howto/sockets
                for conn_to_test in select_connections:
                    try:
                        _, _, _ = gearman.util.select([conn_to_test], [], [], timeout=0)
                    except (select_lib.error, ConnectionError):
                        rd_connections.discard(conn_to_test)
                        wr_connections.discard(conn_to_test)
                        ex_connections.add(conn_to_test)

                        gearman_logger.error('select error: %r' % conn_to_test)

        if _DEBUG_MODE_:
            gearman_logger.debug('select :: Poll - %d :: Read - %d :: Write - %d :: Error - %d', \
                len(select_connections), len(rd_connections), len(wr_connections), len(ex_connections))

        return rd_connections, wr_connections, ex_connections

    def handle_connection_activity(self, rd_connections, wr_connections, ex_connections):
        """Process all connection activity... executes all handle_* callbacks"""
        dead_connections = set()
        for current_connection in rd_connections:
            try:
                self.handle_read(current_connection.fileno())
            except ConnectionError:
                dead_connections.add(current_connection)

        for current_connection in wr_connections:
            try:
                self.handle_write(current_connection.fileno())
            except ConnectionError:
                dead_connections.add(current_connection)

        for current_connection in ex_connections:
            self.handle_error(current_connection.fileno())

        for current_connection in dead_connections:
            self.handle_error(current_connection.fileno())

        failed_connections = ex_connections | dead_connections
        return rd_connections, wr_connections, failed_connections

    def poll_connections_until_stopped(self, submitted_connections, callback_fxn, timeout=None):
        """Continue to poll our connections until we receive a stopping condition"""
        stopwatch = gearman.util.Stopwatch(timeout)

        any_activity = False
        callback_ok = callback_fxn(any_activity)

        connection_ok = compat.any(current_connection.connected or current_connection.connecting for current_connection in submitted_connections)

        while connection_ok and callback_ok:
            time_remaining = stopwatch.get_time_remaining()
            if time_remaining == 0.0:
                break

            # Do a single robust select and handle all connection activity
            read_connections, write_connections, dead_connections = self.poll_connections_once(submitted_connections, timeout=time_remaining)
            self.handle_connection_activity(read_connections, write_connections, dead_connections)

            any_activity = compat.any([read_connections, write_connections, dead_connections])

            callback_ok = callback_fxn(any_activity)
            connection_ok = compat.any(current_connection.connected or current_connection.connecting for current_connection in submitted_connections)

        # We should raise here if we have no alive connections (don't go into a select polling loop with no connections)
        if not connection_ok:
            raise ServerUnavailable('Found no valid connections in list: %r' % self.connection_list)

        return bool(connection_ok and callback_ok)

    def filenos(self):
        connection_fds = set()
        for connection in self.connection_list:
            try:
                connection_fds.add(connection.fileno())
            except ConnectionError:
                pass

        return connection_fds

    def handle_read(self, current_fd):
        """Handle all our pending socket data"""
        current_connection = self.fd_to_connection_map[current_fd]

        # Transfer data from socket -> buffer
        current_connection.handle_read()

    def handle_write(self, current_fd):
        current_connection = self.fd_to_connection_map[current_fd]

        current_connection.handle_write()

    def handle_error(self, current_fd):
        current_connection = self.fd_to_connection_map[current_fd]

        dead_handler = self.connection_to_handler_map.pop(current_connection, None)
        if dead_handler:
            dead_handler.on_io_error()

        current_connection.close()
