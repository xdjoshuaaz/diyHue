class Protocol():
    """Abstract base class for a bulb protocol."""

    def __init__(self):
        raise Exception()

    def get_light_state(self, ip, light):
        """
        For a bulb on *ip*, get its state.

        Parameters
        ----------
        ip : str
            IP address of bulb

        light
            {
                "state": {
                    "on": False,
                    "bri": 200,
                    "hue": 0,
                    "sat": 0,
                    "xy": [0.0, 0.0],
                    "ct": 461,
                    "alert": "none",
                    "effect": "none",
                    "colormode": "ct",
                    "reachable": True
                },
                "type": "Extended color light",
                "name": "MiLight",
                "uniqueid": "1a2b3c4d5e6f",
                "modelid": "LCT001",
                "swversion": "66009461"
            }

        Returns
        -------
        dict
            {   
                "on": False,
                "bri": 200,
                "hue": 0,
                "sat": 0,
                "xy": [0.0,
                0.0],
                "ct": 461,
                "alert": "none",
                "effect": "none",
                "colormode": "ct",
                "reachable": True
            }


        """
        pass

    def set_light(self, ip, light, data):
        """
        For a bulb on *ip", set its state.

        Parameters
        ----------
        ip : str
            IP address of bulb

        light
            {
                "state": {
                    "on": False,
                    "bri": 200,
                    "hue": 0,
                    "sat": 0,
                    "xy": [0.0, 0.0],
                    "ct": 461,
                    "alert": "none",
                    "effect": "none",
                    "colormode": "ct",
                    "reachable": True
                },
                "type": "Extended color light",
                "name": "MiLight",
                "uniqueid": "1a2b3c4d5e6f",
                "modelid": "LCT001",
                "swversion": "66009461"
            }

        data
            {
                "on": False,
                "bri": 200,
                "hue": 0,
                "sat": 0,
                "xy": [0.0, 0.0],
                "ct": 461,
                "alert": "none",
                "effect": "none",
                "colormode": "ct",
                "transitiontime": 3 # in multiples of 100ms
            }

        """
        pass
