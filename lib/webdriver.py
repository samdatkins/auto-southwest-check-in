from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, Dict

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumwire.undetected_chromedriver import Chrome, ChromeOptions

from .general import LoginError

if TYPE_CHECKING:  # pragma: no cover
    from .checkin_scheduler import CheckInScheduler
    from .flight_retriever import AccountFlightRetriever

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:96.0) Gecko/20100101 Firefox/96.0"
BASE_URL = "https://mobile.southwest.com"
LOGIN_URL = BASE_URL + "/api/security/v4/security/token"
TRIPS_URL = BASE_URL + "/api/mobile-misc/v1/mobile-misc/page/upcoming-trips"
CHECKIN_URL = BASE_URL + "/check-in"
RESERVATION_URL = BASE_URL + "/api/mobile-air-operations/v1/mobile-air-operations/page/check-in/"


class WebDriver:
    """
    Controls fetching valid headers for use with the Southwest API.

    This class can be instantiated in two ways:
    1. Setting/refreshing headers before a check in to ensure the headers are valid.
    To do this, the check-in form is filled out with invalid information (valid information
    is not necessary in this case).

    2. Logging into an account. In this case, the headers are refreshed and a list of scheduled
    flights are retrieved.

    Some of this code is based off of:
    https://github.com/byalextran/southwest-headers/commit/d2969306edb0976290bfa256d41badcc9698f6ed
    """

    def __init__(self, checkin_scheduler: CheckInScheduler) -> None:
        self.checkin_scheduler = checkin_scheduler
        self.options = self._get_options()
        self.seleniumwire_options = {"disable_encoding": True}

    def set_headers(self) -> None:
        """
        Fills out a check-in form with invalid information and grabs the valid
        headers from the request. Then, it updates the headers in the check-in scheduler.
        """
        driver = self._get_driver()

        # Attempt a check in to retrieve the correct headers
        confirmation_element = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.NAME, "recordLocator"))
        )
        confirmation_element.send_keys("ABCDEF")  # A valid confirmation number isn't needed

        first_name_element = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.NAME, "firstName"))
        )
        first_name_element.send_keys("John")

        last_name_element = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.NAME, "lastName"))
        )
        last_name_element.send_keys("Doe")
        last_name_element.submit()

        self._set_headers_from_request(driver)
        driver.quit()

    def get_flights(self, flight_retriever: AccountFlightRetriever) -> Dict[str, Any]:
        """
        Logs into the flight retriever account to retrieve a list of scheduled flights.
        Since valid headers are produced, they are also grabbed and updated in the check-in
        scheduler. Last, if the account name is not set, it will set it based on the response
        information.
        """
        driver = self._get_driver()

        # Log in to retrieve the account's trips and needed headers for later requests
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "login-button--box"))
        ).click()

        username_element = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.NAME, "userNameOrAccountNumber"))
        )
        username_element.send_keys(flight_retriever.username)

        password_element = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.NAME, "password"))
        )
        password_element.send_keys(flight_retriever.password)
        password_element.submit()

        self._set_headers_from_request(driver)

        response = driver.requests[0].response
        if response.status_code != 200:
            raise LoginError(str(response.status_code))

        # If this is the first time logging in, the account name needs to be set because that info is needed later
        if flight_retriever.first_name is None:
            response_body = json.loads(response.body)
            self._set_account_name(flight_retriever, response_body)
            print(
                f"Successfully logged in to {flight_retriever.first_name} {flight_retriever.last_name}'s account\n"
            )

        # This page is also loaded when we log in, so we might as well grab it instead of requesting again later
        flights = json.loads(driver.requests[1].response.body)["upcomingTripsPage"]

        driver.quit()

        return [flight for flight in flights if flight["tripType"] == "FLIGHT"]

    def _get_driver(self) -> Chrome:
        chrome_version = self.checkin_scheduler.flight_retriever.config.chrome_version
        driver = Chrome(
            options=self.options,
            seleniumwire_options=self.seleniumwire_options,
            version_main=chrome_version,
        )
        driver.scopes = [LOGIN_URL, TRIPS_URL, RESERVATION_URL]  # Filter out unneeded URLs
        driver.get(CHECKIN_URL)
        return driver

    def _set_headers_from_request(self, driver: Chrome) -> None:
        # Retrieving the headers could fail if the form isn't given enough time to submit
        time.sleep(10)

        request_headers = driver.requests[0].headers
        self.checkin_scheduler.headers = self._get_needed_headers(request_headers)

    def _get_options(self) -> ChromeOptions:
        options = ChromeOptions()
        options.add_argument("--disable-dev-shm-usage")  # For docker containers

        # Southwest detects headless browser user agents, so we have to set our own
        options.add_argument("--user-agent=" + USER_AGENT)

        # This is a temporary workaround for later chrome versions. Currently, the latest
        # version of undetected_chromedriver adds this argument correctly, but it gets
        # detected by Southwest, so this will be here until it can bypass their bot detection.
        chrome_version = self.checkin_scheduler.flight_retriever.config.chrome_version
        if not chrome_version or chrome_version >= 109:
            options.add_argument("--headless=new")
        else:
            options.add_argument("--headless=chrome")

        return options

    @staticmethod
    def _get_needed_headers(request_headers: Dict[str, Any]) -> Dict[str, Any]:
        headers = {}
        for header in request_headers:
            if re.match(r"x-api-key|x-channel-id|user-agent|^[\w-]+?-\w$", header, re.I):
                headers[header] = request_headers[header]

        return headers

    @staticmethod
    def _set_account_name(
        flight_retriever: AccountFlightRetriever, response: Dict[str, Any]
    ) -> None:
        flight_retriever.first_name = response["customers.userInformation.firstName"]
        flight_retriever.last_name = response["customers.userInformation.lastName"]
