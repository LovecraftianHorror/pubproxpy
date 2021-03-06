import json
import os
from datetime import datetime as dt
from time import sleep
from typing import Dict, List, Optional, Set, Tuple, Union, cast
from urllib.parse import urlencode

import requests

from pubproxpy._constants import API_BASE, REQUEST_DELAY
from pubproxpy._singleton import Singleton
from pubproxpy.errors import API_ERROR_MAP, ProxyError
from pubproxpy.types import Level, Params, ParamTypes, Protocol, Proxy


class _FetcherShared(metaclass=Singleton):
    """This class is used solely for the purpose of synchronizing request times and used
    lists between different `ProxyFetcher`s to prevent rate limiting and reusing old
    proxies.
    NOTE: This does not synchronize between threads
    """

    last_requested: Optional[dt]
    used: Set[str]

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.last_requested = None
        self.used = set()


class ProxyFetcher:
    """Class used to fetch proxies from the pubproxy API matching the provided
    parameters
    """

    _exclude_used: bool
    _params: Params
    _proxies: List[Proxy]
    _query: str
    _shared: _FetcherShared

    # Parameters used by `ProxyFetcher` for the pubproxy api
    _PARAMS: Tuple[str, ...] = (
        "api_key",
        "level",
        "protocol",
        "countries",
        "not_countries",
        "last_checked",
        "port",
        "time_to_connect",
        "cookies",
        "google",
        "https",
        "post",
        "referer",
        "user_agent",
    )

    # Parameters that are bounded
    _PARAM_BOUNDS: Dict[str, Tuple[int, int]] = {
        "last_checked": (1, 1000),
        "time_to_connect": (1, 60),
    }

    def __init__(self, *, exclude_used: bool = True, **params: ParamTypes) -> None:
        self._exclude_used = exclude_used

        # Setup `_params` and `_query`
        self._params = self._setup_params(params)
        self._query = f"{API_BASE}{urlencode(self._params)}"

        # List of unused proxies to give
        self._proxies = []

        # Shared data between `ProxyFetcher`s, includes request time and used list
        # (used list only used if `exclude_used` is `True`)
        self._shared = _FetcherShared()

    def _setup_params(self, params: Params) -> Params:
        """Checks all of the params and renames to acutally work with the API"""

        self._verify_params(params)

        # Use API key from env var if passed in
        if "PUBPROXY_API_KEY" in os.environ:
            params["api_key"] = os.environ["PUBPROXY_API_KEY"]

        params = self._rename_params(params)
        return self._format_params(params)

    def _verify_params(self, params: Params) -> None:
        """Since the API really lets anything go, check to make sure params are
        compatible with each other, within the bounds, and are one of the accepted
        options
        """

        # `countries` and `not_countries` are mutually exclusive
        if "countries" in params and "not_countries" in params:
            raise ValueError(
                "incompatible parameters, `countries` and `not_countries` are"
                " mutually exclusive"
            )

        # Check that protocol and level are the correct type
        for key, enum_type in (("protocol", Protocol), ("level", Level)):
            if key in params:
                val = params[key]
                if not isinstance(val, enum_type):
                    raise ValueError(
                        f"{key} should be of type `{enum_type}` not " f" `{type(val)}`"
                    )

        # Verify all params are valid, and satisfy the valid bounds or options
        for param, val in params.items():
            if param not in self._PARAMS:
                raise ValueError(
                    f'unrecognized parameter "{param}" valid parameters are'
                    f" {[p for p in self._PARAMS]}"
                )

            if param in self._PARAM_BOUNDS:
                val = cast(int, val)
                low, high = self._PARAM_BOUNDS[param]
                if val < low or val > high:
                    raise ValueError(
                        f'value "{val}" for "{param}" out of bounds'
                        f" ({low} to {high})"
                    )

    def _rename_params(self, params: Params) -> Params:
        """Method to rename some params from the API's method to pubproxy's
        since some of the API's names are confusing / unclear
        """

        translations = (
            ("api_key", "api"),
            ("protocol", "type"),
            ("countries", "country"),
            ("not_countries", "not_country"),
            ("last_checked", "last_check"),
            ("time_to_connect", "speed"),
        )

        for before, after in translations:
            if before in params:
                params[after] = params[before]
                del params[before]

        return params

    def _format_params(self, params: Params) -> Params:
        """Set any of the always used params and make sure everything is
        `urlencode`able
        """

        # Parameters kept outside of the user's control
        params["format"] = "json"
        if "api" in params:
            params["limit"] = 20
        else:
            params["limit"] = 5

        # Join country and not_country by comma if it's a list or tuple
        if "country" in params:
            if isinstance(params["country"], (list, tuple)):
                params["country"] = ",".join(params["country"])
        elif "not_country" in params:
            if isinstance(params["not_country"], (list, tuple)):
                params["not_country"] = ",".join(params["not_country"])

        # Get value from enums
        for key in ("level", "type"):
            if key in params:
                enum_param = cast(Union[Level, Protocol], params[key])
                params[key] = enum_param.value

        return params

    def drain(self) -> List[Proxy]:
        """Returns any proxies remaining in the current list"""
        return self.get(len(self._proxies))

    def get(self, amount: int = 1) -> List[Proxy]:
        """Attempts to get `amount` proxies matching the specified params"""
        # Remove any blacklisted proxies from the internal list
        # Note: this needs to be done since reused proxies can sit in the internal list
        #       of separate `ProxyFetcher`s
        if self._exclude_used:
            self._proxies = [p for p in self._proxies if p not in self._shared.used]

        # Get enough proxies to satisfy `amount`
        while len(self._proxies) < amount:
            self._proxies += self._fetch()

        # Store the desired proxies in `temp` and remove from `self._proxies`
        temp = self._proxies[:amount]
        self._proxies = self._proxies[amount:]

        # Add the proxies to the blacklist if `_exclude_used`
        if self._exclude_used:
            self._shared.used |= set(temp)

        return temp

    def _fetch(self) -> Set[Proxy]:
        """Attempts to get the proxies from pubproxy.com, will `sleep` to prevent
        getting rate-limited
        """

        # Limit number of requests to 1 per `self._REQUEST_DELAY` unless an API key is
        # provided
        last_time = self._shared.last_requested
        if last_time is not None and "api" not in self._params:
            delta = (dt.now() - last_time).total_seconds()
            if delta < REQUEST_DELAY:
                sleep(REQUEST_DELAY - delta)

        # Query the api
        resp = requests.get(self._query)
        self._shared.last_requested = dt.now()

        try:
            data = json.loads(resp.text)["data"]
        except json.decoder.JSONDecodeError:
            # Try to match on known error message with fallback to unknown error
            raise API_ERROR_MAP.get(resp.text) or ProxyError(resp)

        # Get the returned list of proxies
        proxies = set([d["ipPort"] for d in data])

        # Remove any that were already used and update current list
        if self._exclude_used:
            proxies -= self._shared.used

        return proxies
