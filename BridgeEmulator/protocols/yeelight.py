from concurrent import futures
import json
import logging
import random
import socket
import time
import traceback
from threading import Thread, current_thread
from functools import partial
from collections import namedtuple
import asyncio
import threading
import socketserver
from functions import light_types, nextFreeId
from functions.network import getIpAddress
import sys
from functions.colors import convert_rgb_xy, convert_xy
from .base import Protocol
import copy

# Minimum time between commands in socket mode
MINIMUM_MSEC_BETWEEN_COMMANDS = 300
# Minimum time between commands in music mode
MINIMUM_MSEC_BETWEEN_COMMANDS_MUSIC = 50

# Combine rgb + brightness commands: off (0), set_scene (1), start_flow (2, 50ms OR RAPID_SMOOTH_TRANSITION_TIME)
COMBI_COMMANDS = 0
# If true, use smooth transition instead of sudden when receiving rapid requests (from Hue Entertainment)
RAPID_SMOOTH = True
# If RAPID_SMOOTH is True, what should the smooth transition time be? Set to None to keep value from data
RAPID_SMOOTH_TRANSITION_TIME = 50

CONTINUOUS_TIMER = False

# region Future factory


class FutureFactory:
    """
    Factory that creates future objects for completion by implementors of this class.

    Attributes
    ----------
    persist_after_result : bool
        Determines whether a future remains in the factory after its value is resolved. Useful for futures representing persistent objects such as connections. Default: True

    persist_after_exception : bool
        Determines whether a future remains in the factory after it resolves to an exception. Default: False

    """

    def __init__(self, key_selector):
        """
        Constructor.

        Parameters
        ----------
        key_selector : lambda
            A lambda function taking in an instance, and return a unique key (e.g. an ID)

        """
        self.key_selector = key_selector
        self.futures = {}
        self.future_timeouts = {}
        self.persist_after_result = False
        self.persist_after_exception = False

    def get_or_build(self, key, *args, **kwargs):
        """
        Retrieve an existing future keyed by *key*, or builds a new one.

        Parameters
        ----------
        key
            The key to fetch an existing instance for, or to build a new instance

        args
            Arguments that will be passed to the build method

        kwargs
            Keyword arguments that will be passed to the build method
        """
        future = self.get(key)
        if future is not None:
            return future

        logging.info("%s creating new instance called %s",
                     self.__class__.__name__, key)

        future = futures.Future()
        future.set_running_or_notify_cancel()
        future.add_done_callback(
            lambda f: self.future_done_callback(f, key, *args, **kwargs))
        self.futures[key] = future

        try:
            instance = self.build(key, future, *args, **kwargs)
            if instance is not None:
                future.set_result(instance)
        except Exception as ex:
            future.set_exception(ex)

        return future

    def get(self, key):
        """
        Retrieve an existing future keyed by *key".

        Parameters
        ----------
        key
            The key to fetch an existing instance for.
        """

        if key in self.futures:
            return self.futures[key]

        return None

    def build(self, key, future, *args, **kwargs):
        """
        Build method which is called when a new instance needs to built.
        Implemented by subclasses.

        Parameters
        ----------
        key
            The key that the built instance represents

        future : Future
            The future that will need to completed to "finish" the build and therefore the future, using set_result or set_exception

        args
            Arguments passed by get_or_create

        kwargs
            Keyword arguments passed by get_or_create

        Returns
        -------
        None, or an instance
            Returning anything other than None will complete the future automatically.

        Raises
        ------
        Exception
            Raising an exception will set the future's exception automatically.
        """
        raise NotImplementedError()

    def future_done_callback(self, future, key, dispose=True, *args, **kwargs):
        """
        Called when a future is completed or has raised an exception.
        Cleans up the futures dictionary and cancels timeout timers.

        Parameters
        ----------
        future : Future
            Future that has returned a result or an exception

        key
            Key for the future

        dispose : bool
            If True, the future will be removed from the factory according to persist_after_result and persist_after_exception attributes.

        args
            Arguments passed by get_or_create

        kwargs
            Keyword arguments passed by get_or_create
        """
        try:
            instance = future.result()

            if not self.persist_after_result and dispose:
                if key is None:
                    key = self.key_selector(instance)

                self.instance_disposed(
                    instance=instance, key=id, future=future)
        except Exception as ex:
            if not self.persist_after_exception and dispose:
                self.instance_disposed(key=key, future=future)
        finally:
            self.future_timeout_cleanup(future)

    def instance_disposed(self, instance=None, key=None, future=None):
        """
        Removes a key from the factory's list of futures.

        Parameters
        ----------
        instance
            If specified, this method will only remove futures from the factory if the future's result matches the passed instance.

        key
            If not specified, this method will use the factory's key_selector to determine the key - the instance parameter is required in this case.

        future : Future
            If specified, this method will only remove futures from the factory if the future matches the passed future.
        """
        if key is None and instance is None:
            raise Exception("key and instance is None")

        if key is None:
            key = self.key_selector(instance)

        if key is None:
            raise Exception("Could not find instance")

        if key in self.futures:
            if future is None or self.futures[key] is future:
                if instance is None or (not self.futures[key].running() and not self.futures[key].cancelled() and not self.futures[key].exception() and self.futures[key].result() == instance):
                    found_future = self.futures[key]
                    del self.futures[key]

                    self.future_timeout_cleanup(found_future)

    def future_timeout_cleanup(self, future):
        """
        Cancels timeouts for a given future.

        Parameters
        ----------
        future : Future
            Future to cancel timeouts for
        """
        if future in self.future_timeouts:
            self.future_timeouts[future].cancel()
            del self.future_timeouts[future]

    def future_timeout(self, future, seconds):
        """
        Starts a timeout for a future. After *seconds*, the future will have its exception set to a TimeoutError if it hasn't already been populated.

        Parameters
        ----------
        future : Future
            Future to start a timeout for

        seconds : int
            Number of seconds from now until the future is considered "timed out"
        """
        if future in self.future_timeouts:
            self.future_timeouts[future].cancel()

        self.future_timeouts[future] = threading.Timer(
            seconds, lambda:
            self.future_timedout(future))
        self.future_timeouts[future].start()

    def future_timedout(self, future):
        """
        Called when a timeout started by future_timeout has finished.

        Parameters
        ----------
        future : Future
            Future to set an exception for
        """
        if not future.done():
            future.set_exception(TimeoutError())

        self.future_timeout_cleanup(future)
# endregion

# region Connection factory (handles both types of connection)


class YeelightConnectionFactory():
    """
    Factory that creates new connections and re-uses existing open connections where possible.
    """

    def __init__(self, sockets, music_sockets):
        """
        Constructor.

        Parameters
        ----------
        sockets : SocketConnectionFactory
            A SocketConnectionFactory instance
        music_socket : MusicModeSocketConnectionFactory
            A MusicModeSocketConnectionFactory instance
        """
        self.sockets = sockets
        self.music_sockets = music_sockets

    def get_or_build(self, ip, music=True):
        """
        Retrieve an existing connection to *ip*, or builds a new one.

        Parameters
        ----------
        ip : str
            IP address to retrieve a connection for
        music : bool
            Determines if a music mode connection is suitable
        """
        existing_socket = self.get(ip, music=music)

        if existing_socket is not None:
            return existing_socket

        socket = self.sockets.get_or_build(ip)
        return socket

    def get(self, ip, music=True):
        if music:
            music_socket_future = self.music_sockets.get(ip)
            if music_socket_future is not None and music_socket_future.done():
                return music_socket_future

        socket_future = self.sockets.get(ip)
        if socket_future is not None:
            return socket_future
# endregion

# region Command factory (using Futures)


class CommandFactory(FutureFactory):
    def __init__(self, connection):
        super().__init__(lambda c: c["id"])
        self.connection = connection
        self.connection.response_subscribers.append(
            lambda d: self.receive_command_response(d))
        self.connection.disposed_subscribers.append(
            lambda: self.connection_disposed())

    def get_or_build(self, id, method_name, *params, timeout=5, **kwargs):
        return super().get_or_build(id, method_name, *params, **({"timeout": timeout}))

    def build(self, id, future, method_name, *params, **kwargs):
        timeout = kwargs.get('timeout', 5)
        logging.debug("%s: Invoking command id %d, method %s, params %s",
                      self.connection._id, id, method_name, json.dumps(params))

        self.connection.send((json.dumps(
            {"id": id, "method": method_name, "params": params}) + "\r\n").encode())

        if timeout:
            self.future_timeout(future, timeout)

        return None

    def receive_command_response(self, data):
        if "id" in data:
            if int(data["id"]) in self.futures:
                self.futures[int(data["id"])].set_result(data)

    def connection_disposed(self):
        keys = [k for k in self.futures.keys()]
        for key in keys:
            self.instance_disposed(key=key)
# endregion

# region Regular socket connection class


class SocketConnection():
    def __init__(self, ip, socket, mode='socket'):
        self.ip = ip
        self.socket = socket
        self.mode = mode

        self._next_command_id = 1

        self.response_subscribers = []
        self.disposed_subscribers = []

        self._command_factory = CommandFactory(self)

        self._active_thread = 0
        self._id = '{0}:{1} <- {2} -> {3}:{4}'.format(
            *list(self.socket.getsockname()), self.mode, *list(self.socket.getpeername()))

        logging.info(
            "%s: New yeelight socket", self._id)

    def start(self, threaded_recv=True):
        logging.info(
            "%s: Starting yeelight receive loop, threaded: %s", self._id, threaded_recv)
        if threaded_recv:
            Thread(target=self.recv_loop, name="yeelight-recv-" +
                   self.ip + "#" + str(self._active_thread)).start()
        else:
            self.recv_loop()

    def send(self, *args, **kwargs):
        try:
            logging.debug("%s: Send %s", self._id, args[0].decode('utf8'))
            return self.socket.sendall(*args, **kwargs)
        except Exception as ex:
            logging.exception("%s: Send exception", self._id)
            self.dispose()
            raise

    def recv_loop(self, *args, **kwargs):
        this_thread = self._active_thread + 1
        self._active_thread = this_thread
        logging.info(
            "%s: Started yeelight receive loop, thread id %d", self._id, this_thread)

        while self._active_thread == this_thread:
            try:
                logging.debug("%s: Receiving...", self._id)
                responses = self.socket.recv(16 * 1024)
                logging.debug("%s: Received", self._id)
            except Exception as ex:
                logging.exception("%s: Receive exception", self._id)
                self.dispose()
                raise

            if responses == b'':
                logging.info("%s: Received EOF. Socket has closed.", self._id)
                self.dispose()
                break

            response_list = responses.splitlines()
            for response in response_list:
                r = response.decode("utf8")
                j = json.loads(r)
                logging.debug("%s: Handling response: %s",
                              self._id, json.dumps(r))

                for s in self.response_subscribers:
                    if s(j):
                        break

    def invoke_command(self, method_name, *params):
        command_id = self._next_command_id
        self._next_command_id += 1

        return self._command_factory.get_or_build(command_id, method_name, *params)

    def dispose(self):
        logging.info("%s: Yeelight socket disposed", self._id)

        self.socket.close()

        for s in self.disposed_subscribers:
            if s():
                break
# endregion

# region Music mode socket connection class


class MusicModeSocketConnection(SocketConnection):
    def __init__(self, ip, request):
        super().__init__(ip, request.connection, mode='music')
        self.request = request
        self.socket.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def start(self):
        current_thread().name = "yeelight-recv-music-" + \
            self.ip + "#" + str(self._active_thread)
        super().start(threaded_recv=False)

    def dispose(self):
        self.request.close_connection = True
        self.request.finish()
        super().dispose()

def discover(bridge_config, new_lights):
    group = ("239.255.255.250", 1982)
    message = "\r\n".join([
        'M-SEARCH * HTTP/1.1',
        'HOST: 239.255.255.250:1982',
        'MAN: "ssdp:discover"',
        'ST: wifi_bulb'])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(3)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.sendto(message.encode(), group)
    while True:
        try:
            response = sock.recv(1024).decode('utf-8').split("\r\n")
            properties = {"rgb": False, "ct": False}
            for line in response:
                #logging.info("line check: " + line)
                if line[:2] == "id":
                    properties["id"] = line[4:]
                elif line[:3] == "rgb":
                    properties["rgb"] = True
                elif line[:2] == "ct":
                    properties["ct"] = True
                elif line[:8] == "Location":
                    properties["ip"] = line.split(":")[2][2:]
                elif line[:4] == "name":
                    properties["name"] = line[6:]
                elif line[:5] == "model":
                    properties["model"] = line.split(": ",1)[1]
            device_exist = False
            for light in bridge_config["lights_address"].keys():
                if bridge_config["lights_address"][light]["protocol"] == "yeelight" and bridge_config["lights_address"][light]["id"] == properties["id"]:
                    device_exist = True
                    bridge_config["lights_address"][light]["ip"] = properties["ip"]
                    logging.debug(
                        "light id " + properties["id"] + " already exist, updating ip...")
                    break
            if (not device_exist):
                #light_name = "YeeLight id " + properties["id"][-8:] if properties["name"] == "" else properties["name"]
                light_name = "Yeelight " + properties["model"] + " " + properties["ip"][-3:] if properties["name"] == "" else properties["name"] #just for me :)
                logging.debug("Add YeeLight: " + properties["id"])
                modelid = "LWB010"
                if properties["model"] == "desklamp":
                    modelid = "LTW001"
                elif properties["rgb"]:
                    modelid = "LCT015"
                elif properties["ct"]:
                    modelid = "LTW001"
                new_light_id = nextFreeId(bridge_config, "lights")
                bridge_config["lights"][new_light_id] = {"state": light_types[modelid]["state"], "type": light_types[modelid]["type"], "name": light_name, "uniqueid": "4a:e0:ad:7f:cf:" + str(
                    random.randrange(0, 99)) + "-1", "modelid": modelid, "manufacturername": "Philips", "swversion": light_types[modelid]["swversion"]}
                new_lights.update({new_light_id: {"name": light_name}})
                bridge_config["lights_address"][new_light_id] = {
                    "ip": properties["ip"], "id": properties["id"], "protocol": "yeelight"}

        except socket.timeout:
            logging.debug('Yeelight search end')
            sock.close()
            break
# endregion

# region Protocol HTTP/TCP request handler delegate (For music mode socket handling)


class YeelightRequestHandlerDelegate():
    """
    Class that is instantiated when the bridge's HTTP server has an incoming connection from an IP address representing a Yeelight bulb on the bridge.
    """

    def __init__(self, protocol, request_handler):
        """
        Constructor.

        Parameters
        ----------
        protocol : YeelightProtocol
            Instance of Yeelight protocol class
        request_handler : BaseHTTPRequestHandler
            Request handler instance that started handling the incoming connection on the HTTP server.
        """

        self.protocol = protocol
        self.request_handler = request_handler

    def handle(self):
        """
        Handles an incoming connection (after BaseHTTPRequestHandler.setup(), before BaseHTTPRequestHandler().finish())

        Returns
        -------
        True
            This prevents the BaseHTTPRequestHandler's handle() method from being called, stopping it treating the connection as a HTTP request which closes the connection due to it not being HTTP.
        """

        # Bulb is creating a TCP socket with the server
        ip = self.request_handler.client_address[0]

        logging.info("%s incoming connection by music mode", ip)
        music_mode_connection = self.protocol.connection_factory.music_sockets.get(ip)
        if music_mode_connection is not None and music_mode_connection.done() and not music_mode_connection.cancelled():
            try:
                if music_mode_connection.result().request == self.request_handler:
                    pass
                else:
                    logging.info(
                        "%s already has music mode connection, is trying to connect again", ip)
                    music_mode_connection.result().dispose()
            except:
                pass  # connection in error state shouldn't happen
        
        self.request_handler.close_connection = False

        connection = MusicModeSocketConnection(ip, self.request_handler)
        self.protocol.connection_factory.music_sockets.connection_established(
            connection)
        connection.start()


        return True
# endregion

# region Regular socket connection factory (using Futures)


class SocketConnectionFactory(FutureFactory):
    """
    Factory that creates new connections to Yeelight bulbs over port 55443.
    """

    def __init__(self):
        super().__init__(lambda s: s.ip)
        self.persist_after_result = True

    def build(self, ip, future):
        """
        Build a new connection to *ip*.

        Parameters
        ----------
        ip: str
            IP address to connect to
        """
        try:
            tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_socket.settimeout(None)
            tcp_socket.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
            tcp_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            tcp_socket.connect((ip, int(55443)))
            connection = SocketConnection(ip, tcp_socket)
            try:
                connection.disposed_subscribers.append(
                    lambda: self.instance_disposed(instance=connection, key=ip, future=future))
                connection.start()
            except:
                connection.dispose()
                raise

            return connection
        except:
            logging.exception("Connection to %s failed: ", ip)
            raise
# endregion

# region Music mode socket connection (using Futures)


class MusicModeSocketConnectionFactory(SocketConnectionFactory):
    """
    Factory that attempts to establish a music mode connection to a Yeelight bulb using an existing SocketConnection instance.
    """

    def __init__(self):
        super().__init__()

    def get_or_build(self, connection, timeout=5):
        """
        Retrives an existing music mode connection, or returns a future possibly establishing a music mode connection later on.

        Parameters
        ----------
        ip: str
            IP address to connect to
        connection: SocketConnection
            Existing socket connection to send music mode on message over
        """
        return super().get_or_build(key=connection.ip, connection=connection, timeout=timeout)

    def future_done_callback(self, future, key, *args, connection, **kwargs):
        """
        Overriden super's future_done_callback for logging.
        """
        try:
            future.result()
        except Exception as ex:
            logging.exception(
                "Set music on command for %s failed", connection.ip)

        super().future_done_callback(future, key)

    def connection_established(self, connection):
        """
        Call when the bulb has established a TCP socket to the server over * connection*.

        Parameters
        ----------
        connection: MusicModeConnection
            Connection over which the music mode connection was established
        """
        key = self.key_selector(connection)

        connection.disposed_subscribers.append(
            lambda: self.instance_disposed(instance=connection, key=key))

        if key in self.futures:
            self.futures[key].set_result(connection)
        else:
            connection.dispose()

    def build(self, ip, future, *args, connection, timeout, **kwargs):
        """
        Attempts to build a music mode connection by dispatching set_music command over * connection*.

        Parameters
        ----------
        ip: str
            Bulb IP(should match connection.ip)

        future: Future
            Future to resolve when connection is established

        connection: SocketConnection
            SocketConnection instance to dispatch command over

        timeout: int
            Seconds before music mode connection attempt times out
        """
        server_ip = getIpAddress()
        logging.info("Setting music on for %s (to %s)",
                     connection.ip, server_ip)

        connection.invoke_command("set_music", 0)

        command = connection.invoke_command(
            "set_music", 1, server_ip, 80)
        command.add_done_callback(
            lambda f: self.command_future_done_callback(f, ip, future, connection=connection))

        if timeout:
            self.future_timeout(future, timeout)

        return None

    def command_future_done_callback(self, future, ip, connection_future, connection):
        """
        Listens for result of set_music command and reacts accordingly.

        Parameters
        ----------
        future: Future
            Command future containing result or exception(is done)
        ip: str
            IP address of bulb
        connection_future: Future
            Music mode connection attempt future(from build)
        connection: SocketConnection
            Connection that command was sent on
        """
        try:
            data = future.result()

            logging.info("Set music on command for %s returned %s",
                         connection.ip, json.dumps(data))

            if "error" in data:
                raise Exception(json.dumps(data))
        except Exception as ex:
            connection_future.set_exception(ex)
# endregion


def scale(value, src, dst):
    return ((dst[1] - dst[0]) * (value - src[0]) / (src[1] - src[0])) + dst[0]


class YeelightProtocol(Protocol):
    """
    Class containing an implementation of Protocol for Yeelight bulbs.
    """
    __name__ = "protocols.yeelight"

    def __init__(self):
        self._sockets = SocketConnectionFactory()
        self._music_sockets = MusicModeSocketConnectionFactory()
        self._queued_set_light_data = {}

        self.connection_factory = YeelightConnectionFactory(
            self._sockets, self._music_sockets)

        self.RequestHandlerDelegateClass = YeelightRequestHandlerDelegate

        self.event_loop = asyncio.new_event_loop()
        Thread(name="yeelight-command-queue",
               target=self.event_loop.run_forever).start()

# region Protocol implementation
    def get_light_state(self, address, light):
        state = {}
        try:
            connection = self.connection_factory.get_or_build(
                address["ip"], music=False).result(5)
        except Exception as ex:
            raise

        data = connection.invoke_command(
            "get_prop", "power", "bright").result(3)
        light_data = data["result"]

        if light_data[0] == "on":  # powerstate
            state['on'] = True
        else:
            state['on'] = False
        state["bri"] = int(int(light_data[1]) * 2.54)

        if light["name"].find("desklamp") > 0:
            data = connection.invoke_command("get_prop", "ct").result(3)
            tempval = int(1000000 / int(data["result"][0]))
            if tempval > 369: tempval = 369
            state["ct"] = tempval # int(1000000 / int(json.loads(data[:-2].decode("utf8"))["result"][0]))
            state["colormode"] = "ct"
        else:
            data = connection.invoke_command("get_prop", "color_mode").result(3)
            if data["result"][0] == "1":  # rgb mode
                data = connection.invoke_command("get_prop", "rgb").result(3)
                hue_data = data["result"]
                hex_rgb = "%6x" % int(hue_data[0])
                r = hex_rgb[: 2]
                if r == "  ":
                    r = "00"
                g = hex_rgb[3: 4]
                if g == "  ":
                    g = "00"
                b = hex_rgb[-2:]
                if b == "  ":
                    b = "00"
                state["rgb"] = int(hue_data[0])
                state["xy"] = convert_rgb_xy(int(r, 16), int(g, 16), int(b, 16))
                state["colormode"] = "xy"
            elif data["result"][0] == "2":  # ct mode
                data = connection.invoke_command("get_prop", "ct").result(3)
                state["ct"] = int(
                    1000000 / int(data["result"][0]))
                state["colormode"] = "ct"

            elif data["result"][0] == "3":  # ct mode
                data = connection.invoke_command(
                    "get_prop", "hue", "sat").result(3)
                hue_data = data["result"]
                state["hue"] = int(hue_data[0] * 182)
                state["sat"] = int(int(hue_data[1]) * 2.54)
                state["colormode"] = "hs"
        return state

    def set_light(self, address, light, data):
        if "transitiontime" in data:
            data["transitiontime"] = data["transitiontime"] * 100

        self.enqueue_set_light_data(address["ip"], light, data)
# endregion

# region Command queueing methods
    def enqueue_set_light_data(self, ip, light, data):
        if ip not in self._queued_set_light_data:
            self._queued_set_light_data[ip] = {
                "timestamp": 0,
                "data": {},
                "timer": None,
                "count": 0,
                "delay": None,
                "prev": {}
            }

        queued_data = self._queued_set_light_data[ip]

        if data is not None:
            queued_data["data"].update(data)

        callback = partial(self.dequeue_set_light_data, ip, light,
                           queued_data)

        now = self.event_loop.time() * 1000
        delay = self.minimum_msec_between_commands_for_ip(ip)
        last_time = queued_data["timestamp"]

        queued_data["count"] += 1

        if queued_data["timer"] is None:
            if now - last_time > delay:
                logging.debug(
                    "last set_light call for %s was more than %f ms ago (was %f ms), invoking now", ip, delay, now - last_time)
                queued_data["timer"] = self.event_loop.call_soon_threadsafe(
                    callback)
            else:
                logging.debug("last set_light call for %s was %f ms ago, invoking in %f",
                              ip, now - last_time, delay - now + last_time)
                queued_data["delay"] = delay
                queued_data["timer"] = self.event_loop.call_soon_threadsafe(
                    lambda: self.event_loop.call_at((last_time + delay) / 1000, callback))
        else:
            if now - last_time > 3000:
                # Watchdog: if future was more than 3s ago, and still not executed, clear it
                logging.debug(
                    "last set_light call for %s was %f ms ago, already queued but dequeuing anyway due to possible failure to execute", ip, now - last_time)
                queued_data["timer"] = self.event_loop.call_soon_threadsafe(
                    callback)
            else:
                logging.debug(
                    "last set_light call for %s was %f ms ago, already queued", ip, now - last_time)

    def dequeue_set_light_data(self, ip, light, data):
        run_again = True
        try:
            self.run_commands(ip, light, data)
        except:
            run_again = False
        finally:
            now = self.event_loop.time() * 1000
            delay = self.minimum_msec_between_commands_for_ip(ip)
            data["timestamp"] = now
            data["prev"].update(data["data"])
            data["data"] = {}
            data["timer"] = None
            data["count"] = 0
            data["delay"] = delay
            data["timer"] = self.event_loop.call_at(
                (now + delay) / 1000, partial(self.dequeue_set_light_data, ip, light, data)) if run_again and CONTINUOUS_TIMER else None
# endregion

# region Command executor
    def run_commands(self, ip, light, command_data):
        data = command_data["data"]
        if not command_data["data"]:
            return

        try:
            connection = self.connection_factory.get_or_build(ip).result(5)
        except Exception as ex:
            raise

        if True or (command_data["delay"] is not None and connection.mode != "music" and command_data["count"] >= 3):
            # More than 3 requests attempted within a small amount of time? turn music mode on
            try:
                self.connection_factory.music_sockets.get_or_build(connection)
            except Exception as ex:
                pass

        payload = self.convert_to_payload(data, light, command_data["prev"])

        if not payload:
            return

        msg = ""
        for (method, params) in payload.items():
            msg += json.dumps(
                {"id": 0, "method": method, "params": params}) + "\r\n"

        try:
            connection.send(msg.encode())
        except Exception as ex:
            logging.exception("Command send exception")
            if connection.mode == 'music':
                logging.info("Retrying command on another connection")
                return self.run_commands(ip, light, data)
            else:
                raise
# endregion

# region Command queue interval method
    def minimum_msec_between_commands_for_ip(self, ip):
        connection = self.connection_factory.get(ip)

        if connection is not None and connection.result().mode == 'music':
            return MINIMUM_MSEC_BETWEEN_COMMANDS_MUSIC

        return MINIMUM_MSEC_BETWEEN_COMMANDS
# endregion


# region Hue data to Yeelight payload converter method

    def convert_to_payload(self, data, light, prev, updated_vals_only=True):
        payload = {}

        sudden = ["linear", 50]

        will_transition = "rapid" not in data or RAPID_SMOOTH
        transition = sudden

        if not prev:
            updated_vals_only = False  # first update

        if will_transition:
            transition = [
                "linear",
                RAPID_SMOOTH_TRANSITION_TIME if "rapid" in data and RAPID_SMOOTH and RAPID_SMOOTH_TRANSITION_TIME is not None else max(50, data.get("transitiontime", 400))]

        if "on" in data and "rapid" not in data:
            if data["on"]:
                payload["set_power"] = ["on", *transition]
            else:
                payload["set_power"] = ["off", *transition]

        if "bri" in data:
            # range(1, 255) from hue (max: 254)
            # range(1, 101) to yeelight (max: 100)
            i = (1, 254)
            o = (1, 100)
            bri = int(scale(data["bri"], i, o))
            if not updated_vals_only or int(scale(prev.get("bri", 0), i, o)) != bri:
                payload["set_bright"] = [bri, *transition]

        if "ct" in data:
            #if ip[:-3] == "201" or ip[:-3] == "202":
            if light["name"].find("desklamp") > 0:
                if data["ct"] > 369: data["ct"] = 369
            payload["set_ct_abx"] = [
                int(1000000 / data["ct"]), *transition]

        if "hue" in data and "sat" in data:
            payload["set_hsv"] = [
                int(data["hue"] / 182), int(data["sat"] / 2.54), *transition]
        elif "hue" in data and "sat" not in data:
            payload["set_hsv"] = [
                int(data["hue"] / 182), int(prev["sat"] / 2.54), *transition]
        elif "hue" in data and "sat" not in data:
            payload["set_hsv"] = [
                int(prev["hue"] / 182), int(data["sat"] / 2.54), *transition]

        if "rgb" in data:
            rgb = data["rgb"]
            color = (rgb[0] * 65536) + (rgb[1] * 256) + rgb[2]

            prev_rgb = prev.get("rgb", (-1, -1, -1)
                                ) if updated_vals_only else None

            if not updated_vals_only or rgb != prev_rgb:
                payload["set_rgb"] = [color, *transition]

        elif "xy" in data:  # prefer rgb if possible over xy
            if "bri" in data:
                bri = data["bri"]
            else:
                bri = prev["bri"]

            color = convert_xy(data["xy"][0], data["xy"][1], bri)
            # according to docs, yeelight needs this to set rgb. its r * 65536 + g * 256 + b
            payload["set_rgb"] = [
                (color[0] * 65536) + (color[1] * 256) + color[2], *transition]

        if "alert" in data and data["alert"] != "none":
            payload["start_cf"] = [
                4, 0, "1000, 2, 5500, 100, 1000, 2, 5500, 1, 1000, 2, 5500, 100, 1000, 2, 5500, 1"]

        if COMBI_COMMANDS >= 1 and "set_rgb" in payload and "set_bright" in payload:
            if COMBI_COMMANDS == 1:
                payload["set_scene"] = [
                    "color", payload["set_rgb"][0], payload["set_bright"][0]]
            elif COMBI_COMMANDS == 2:
                payload["start_cf"] = [1, 1, "{0},1,{1},{2}".format(min(transition[1], 50) if will_transition else 50,
                                                                    payload["set_rgb"][0], payload["set_bright"][0])]

            del payload["set_rgb"]
            del payload["set_bright"]

        return payload
# endregion
