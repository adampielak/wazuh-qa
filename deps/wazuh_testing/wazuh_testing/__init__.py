# Copyright (C) 2015-2020, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is free software; you can redistribute it and/or modify it under the terms of GPLv2

import sys
from collections import defaultdict


class Parameters:
    """Class to allocate all global parameters for testing"""

    def __init__(self):
        timeouts = defaultdict(lambda: 10)
        timeouts['linux'] = 5
        timeouts['darwin'] = 5
        self._default_timeout = timeouts[sys.platform]

    @property
    def default_timeout(self):
        """
        Getter method for the default timeout property

        Returns
        -------
        int representing the default timeout in seconds
        """
        return self._default_timeout

    @default_timeout.setter
    def default_timeout(self, value):
        """
        Setter method for the default timeout property

        Parameters
        ----------
        value : int
            New value for the default timeout. Must be in seconds.
        """
        self._default_timeout = value


global_parameters = Parameters()
