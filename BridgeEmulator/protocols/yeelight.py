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

from functions import light_types, nextFreeId
from functions.colors import convert_rgb_xy, convert_xy
from HueEmulator3 import getIpAddress


class SocketConnection():
    def __init__(self, ip, socket, mode='socket'):
        self.ip = ip
        self.socket = socket
        self.mode = mode

        self._pending_commands = {}
        self._next_command_id = 1
        self._active_thread = 0

        print("ctor " + str(id(self)))

    def start(self, threaded_recv=True):
        if threaded_recv:
            Thread(target=self.recv_loop, name="yeelight-recv-" +
                   self.ip + "#" + str(self._active_thread)).start()
        else:
            self.recv_loop()

    def send(self, *args, **kwargs):
        try:
            return self.socket.sendall(*args, **kwargs)
        except ConnectionError as ex:
            print("t")
            self.dispose()
            raise

    def recv(self, *args, **kwargs):
        return self.socket.recv(*args, **kwargs)

    def recv_loop(self, *args, **kwargs):
        this_thread = self._active_thread + 1
        self._active_thread = this_thread
        print("recv loop start on " + str(this_thread))
        while self._active_thread == this_thread:
            try:
                print("recv start mode " + self.mode)
                responses = self.recv(16 * 1024)
                print("recv " + str(responses))
                print("mode " + self.mode)
            except ConnectionError as ex:
                self.dispose()
                raise

            if responses == b'':
                self._cancel_pending_command_invocations_with_exception(None)
                break

            response_list = responses.splitlines()
            for response in response_list:
                j = json.loads(response.decode("utf8"))

                if "id" in j:
                    print(str(j["id"]) + " " + json.dumps(j) + " " +
                          json.dumps(list(self._pending_commands.keys())))
                    self._set_command_invocation_result(int(j["id"]), j)

    def _cancel_pending_command_invocations_with_exception(self, ex):
        pending_commands = self._pending_commands
        self._pending_commands = {}

        for (command_id, future) in pending_commands.items():
            future.set_exception(ex)

    def _set_command_invocation_result(self, command_id, *args):
        if command_id in self._pending_commands:
            future = self._pending_commands[command_id]
            del self._pending_commands[command_id]
            future.set_result(*args)

    def invoke_command(self, method_name, *params):
        command_id = self._next_command_id
        self._next_command_id += 1

        future = futures.Future()
        future.set_running_or_notify_cancel()
        self._pending_commands[command_id] = future

        def done_callback(f):
            print("done " + str(command_id))
            if command_id in self._pending_commands:
                del self._pending_commands[command_id]

        future.add_done_callback(done_callback)

        print("sending " + str(command_id))
        self.send((json.dumps(
            {"id": command_id, "method": method_name, "params": params}) + "\r\n").encode())

        return future

    def dispose(self):
        self.socket.close()

        if self.ip in socket_connections:
            del socket_connections[self.ip]

        if self.ip in socket_connection_attempts:
            del socket_connection_attempts[self.ip]


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
        self.socket.close()

        traceback.print_stack()

        self.request.finish(force=True)

        if self.ip in music_mode_connections:
            del music_mode_connections[self.ip]


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
            device_exist = False
            for light in bridge_config["lights_address"].keys():
                if bridge_config["lights_address"][light]["protocol"] == "yeelight" and bridge_config["lights_address"][light]["id"] == properties["id"]:
                    device_exist = True
                    bridge_config["lights_address"][light]["ip"] = properties["ip"]
                    logging.debug(
                        "light id " + properties["id"] + " already exist, updating ip...")
                    break
            if (not device_exist):
                light_name = "YeeLight id " + \
                    properties["id"][-8:] if properties["name"] == "" else properties["name"]
                logging.debug("Add YeeLight: " + properties["id"])
                modelid = "LWB010"
                if properties["rgb"]:
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


music_mode_connections = {}
socket_connections = {}
socket_connection_attempts = {}
queued_set_light_data = {}


def handle_request(request_handler):
    # Bulb is creating a TCP socket with the server
    ip = request_handler.client_address[0]

    if ip in music_mode_connections:
        music_mode_connections[ip].dispose()

    music_mode_connections[ip] = MusicModeSocketConnection(ip, request_handler)
    music_mode_connections[ip].start()

    return False


def new_connection(ip):
    while ip in socket_connection_attempts:
        time.sleep(1)

    if ip in socket_connections:
        return socket_connections[ip]

    tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    connection = SocketConnection(ip, tcp_socket)
    socket_connection_attempts[ip] = True

    tcp_socket.settimeout(None)
    tcp_socket.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
    tcp_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    try:
        connection.socket.connect((ip, int(55443)))
        connection.start()

        socket_connections[ip] = connection
        if ip in socket_connection_attempts:
            del socket_connection_attempts[ip]

        set_music_on(connection)
    except:
        connection.dispose()
        raise

    return connection


def set_music_on(connection):
    ##print("set_music off result: " + json.dumps(connection.invoke_command("set_music", 0).result(1)))

    time.sleep(1)

    print("set_music on result: " +
          json.dumps(connection.invoke_command("set_music", 1, getIpAddress(), 80).result(5)))


def get_existing_connection(ip, music=True, socket=True):
    if ip in music_mode_connections and music:
        return music_mode_connections[ip]
    elif ip in socket_connections and socket:
        return socket_connections[ip]
    else:
        return None


def get_or_create_connection(ip, music=True, socket=True):
    connection = get_existing_connection(ip, music, socket)

    if connection is not None:
        print(connection.mode)
        return connection

    print('new')
    return new_connection(ip)


COLOR_FLOW_TIME = 15
MINIMUM_MSEC_BETWEEN_COMMANDS = COLOR_FLOW_TIME
TRANSITION = ["smooth", COLOR_FLOW_TIME]


def queue_set_light_data(ip, data):
    if ip not in queued_set_light_data:
        queued_set_light_data[ip] = {
            "timestamp": 0,
            "data": {}
        }

    now = time.time_ns() / 1000000
    diff = now - queued_set_light_data[ip]["timestamp"]

    if data is not None:
        queued_set_light_data[ip]["data"].update(data)

    if diff > MINIMUM_MSEC_BETWEEN_COMMANDS:
        data = queued_set_light_data[ip]["data"]
        queued_set_light_data[ip]["data"] = {}
        queued_set_light_data[ip]["timestamp"] = now

        print("calling ({2}ms) {0}, {1}".format(ip, json.dumps(data), diff))

        return (True, data)
    else:
        print("queued call to {0}, last {1}ms ago, {2}".format(
              ip, diff, json.dumps(data)))
        return (False, None)


def commands(ip, payload):
    try:
        connection = get_or_create_connection(ip)
    except Exception as ex:
        raise

    msg = ""
    for (method, params) in payload.items():
        msg += json.dumps(
            {"id": 0, "method": method, "params": params}) + "\r\n"

    try:
        connection.send(msg.encode())
    except Exception as ex:
        logging.exception("command(): Unexpected error")
        if connection.mode == 'music':
            return commands(ip, payload)
        else:
            raise


def set_light(ip, light, data):
    method = 'TCP'
    payload = {}
    transitiontime = 400
    if "transitiontime" in data:
        transitiontime = data["transitiontime"] * 10

    (ready, ready_data) = queue_set_light_data(ip, data)
    if not ready:
        return

    for key, value in ready_data.items():
        if key == "on":
            if value:
                payload["set_power"] = ["on", *TRANSITION]
            else:
                payload["set_power"] = ["off", *TRANSITION]
        elif key == "bri" and ("xy" not in ready_data or "rgb" in ready_data):
            payload["set_bright"] = [
                int(value / 2.55) + 1, *TRANSITION]
        elif key == "ct":
            payload["set_ct_abx"] = [
                int(1000000 / value), *TRANSITION]
        elif key == "hue":
            payload["set_hsv"] = [
                int(value / 182), int(light["state"]["sat"] / 2.54), *TRANSITION]
        elif key == "sat":
            payload["set_hsv"] = [
                int(value / 2.54), int(light["state"]["hue"] / 2.54), *TRANSITION]
        elif key == "xy" and "rgb" not in ready_data:
            if "bri" in ready_data:
                bri = ready_data["bri"]
            else:
                bri = light["state"]["bri"]

            color = convert_xy(value[0], value[1], bri)
            # according to docs, yeelight needs this to set rgb. its r * 65536 + g * 256 + b
            payload["set_rgb"] = [
                (color[0] * 65536) + (color[1] * 256) + color[2], *TRANSITION]

        elif key == "rgb":
            color = value
            payload["set_rgb"] = [
                (color[0] * 65536) + (color[1] * 256) + color[2], *TRANSITION]

        elif key == "alert" and value != "none":
            payload["start_cf"] = [
                4, 0, "1000, 2, 5500, 100, 1000, 2, 5500, 1, 1000, 2, 5500, 100, 1000, 2, 5500, 1"]

    # if "set_rgb" in payload and "set_bright" in payload:
    #     if False:
    #         payload["start_cf"] = [1, 1,
    #                                "{0}, 1, {1}, {2}".format(50, payload["set_rgb"][0], payload["set_bright"][0])]
    #     else:
    #         payload["set_scene"] = [
    #             "color", payload["set_rgb"][0], payload["set_bright"][0]]
    #     del payload["set_rgb"]
    #     del payload["set_bright"]

    commands(ip, payload)


def get_light_state(ip, light):
    state = {}
    try:
        connection = get_or_create_connection(ip, music=False)
    except Exception as ex:
        raise

    data = connection.invoke_command("get_prop", "power", "bright").result(3)
    light_data = data["result"]

    if light_data[0] == "on":  # powerstate
        state['on'] = True
    else:
        state['on'] = False
    state["bri"] = int(int(light_data[1]) * 2.54)

    data = connection.invoke_command("get_prop", "color_mode").result(3)
    if data["result"][0] == "1":  # rgb mode
        data = connection.invoke_command("get_prop", "rgb").result(3)
        hue_data = data["result"]
        hex_rgb = "%6x" % int(hue_data[0])
        r = hex_rgb[:2]
        if r == "  ":
            r = "00"
        g = hex_rgb[3:4]
        if g == "  ":
            g = "00"
        b = hex_rgb[-2:]
        if b == "  ":
            b = "00"
        state["xy"] = convert_rgb_xy(int(r, 16), int(g, 16), int(b, 16))
        state["colormode"] = "xy"
    elif data["result"][0] == "2":  # ct mode
        data = connection.invoke_command("get_prop", "ct").result(3)
        state["ct"] = int(
            1000000 / int(data["result"][0]))
        state["colormode"] = "ct"

    elif data["result"][0] == "3":  # ct mode
        data = connection.invoke_command("get_prop", "hue", "sat").result(3)
        hue_data = data["result"]
        state["hue"] = int(hue_data[0] * 182)
        state["sat"] = int(int(hue_data[1]) * 2.54)
        state["colormode"] = "hs"
    return state
