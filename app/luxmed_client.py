"""Pure Luxmed Patient Portal API client.

Extracted from luxmed_sniper.py: login + bearer token, appointment search,
dictionary endpoints. No config files, no notifications, no persistence —
plain parameters in, parsed data out.
"""
import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests
from loguru import logger


class LuxmedError(Exception):
    pass


class LuxmedAuthError(LuxmedError):
    pass


@dataclass(frozen=True)
class Appointment:
    date: datetime.datetime
    clinic_id: int
    clinic: str
    doctor: str
    service_id: int


class LuxmedClient:
    BASE_URL = "https://portalpacjenta.luxmed.pl/PatientPortal"
    LOGIN_URL = f"{BASE_URL}/Account/LogIn"
    TERMS_URL = f"{BASE_URL}/NewPortal/terms/index"
    DICTIONARY_URL = f"{BASE_URL}/NewPortal/Dictionary"

    def __init__(
        self,
        login: str,
        password: str,
        timeout: int = 30,
        on_request: Callable[[], None] | None = None,
    ):
        self._login = login
        self._password = password
        self.timeout = timeout
        # Called before every HTTP request — lets the caller enforce
        # a daily request budget (raises to abort).
        self.on_request = on_request
        self.session = requests.Session()
        self.logged_in_at: datetime.datetime | None = None

    def _before_request(self) -> None:
        if self.on_request is not None:
            self.on_request()

    def log_in(self) -> None:
        self._before_request()
        response = self.session.post(
            url=self.LOGIN_URL,
            json={"login": self._login, "password": self._password},
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code != requests.codes["ok"]:
            raise LuxmedAuthError(f"Login failed with HTTP {response.status_code}")

        self.session.cookies = response.cookies
        for k, v in self.session.cookies.items():
            self.session.headers.update({k: v})

        try:
            token = response.json()["token"]
        except (ValueError, KeyError) as e:
            raise LuxmedAuthError("Login response did not contain a token") from e
        self.session.headers["authorization-token"] = f"Bearer {token}"
        self.logged_in_at = datetime.datetime.now(tz=datetime.UTC)
        logger.info("Logged in to Luxmed portal")

    def ensure_logged_in(self, max_age_minutes: int = 10) -> None:
        """Re-login when the session is missing or older than max_age_minutes."""
        if self.logged_in_at is None:
            self.log_in()
            return
        age = datetime.datetime.now(tz=datetime.UTC) - self.logged_in_at
        if age > datetime.timedelta(minutes=max_age_minutes):
            self.log_in()

    def _get_json(self, url: str, params: dict | None = None):
        self._before_request()
        response = self.session.get(
            url=url,
            params=params,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            raise LuxmedAuthError(f"Session rejected (HTTP {response.status_code})")
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as e:
            # Portal answers HTML (login page) when the session died silently.
            raise LuxmedAuthError("Non-JSON response — session probably expired") from e

    def search_appointments(
        self,
        city_id: int,
        service_id: int,
        clinic_ids: list[int] | None = None,
        doctor_ids: list[int] | None = None,
        lookup_days: int = 14,
    ) -> list[Appointment]:
        now = datetime.datetime.now(tz=datetime.UTC)
        date_to = now + datetime.timedelta(days=lookup_days)

        params: dict = {
            "searchPlace.id": city_id,
            "searchPlace.type": 0,
            "serviceVariantId": service_id,
            "languageId": 10,
            "searchDateFrom": now.date().strftime("%Y-%m-%d"),
            "searchDateTo": date_to.strftime("%Y-%m-%d"),
            "searchDatePreset": lookup_days,
            "delocalized": "false",
            "processId": str(uuid.uuid4()),
        }
        if clinic_ids:
            params["facilitiesIds"] = clinic_ids
        if doctor_ids:
            params["doctorsIds"] = doctor_ids

        content = self._get_json(self.TERMS_URL, params=params)

        appointments: list[Appointment] = []
        for term_for_day in content["termsForService"]["termsForDays"]:
            for term in term_for_day["terms"]:
                doctor = term["doctor"]
                clinic_group_id = int(term["clinicGroupId"])
                doctor_id = int(doctor["id"])

                if doctor_ids and doctor_id not in doctor_ids:
                    continue
                if clinic_ids and clinic_group_id not in clinic_ids:
                    continue

                date = datetime.datetime.fromisoformat(term["dateTimeFrom"]).replace(
                    tzinfo=ZoneInfo("Europe/Warsaw"),
                )
                if date > date_to:
                    continue
                appointments.append(
                    Appointment(
                        date=date,
                        clinic_id=term["clinicId"],
                        clinic=term["clinic"],
                        doctor=f"{doctor['academicTitle']} {doctor['firstName']} {doctor['lastName']}".strip(),
                        service_id=term["serviceId"],
                    ),
                )
        return appointments

    # --- dictionaries (dropdown data for the GUI) ---

    def get_cities(self) -> list[dict]:
        return self._get_json(f"{self.DICTIONARY_URL}/cities")

    def get_services(self) -> list[dict]:
        """Service variant tree flattened to [{id, name, path, telemedicine}]."""
        flat: list[dict] = []

        def walk(nodes: list[dict], path: list[str]) -> None:
            for node in nodes:
                full_path = [*path, node["name"]]
                flat.append({
                    "id": node["id"],
                    "name": node["name"],
                    "path": " › ".join(full_path),
                    "telemedicine": node.get("isTelemedicine", False),
                })
                walk(node.get("children") or [], full_path)

        walk(self._get_json(f"{self.DICTIONARY_URL}/serviceVariantsGroups"), [])
        return flat

    def get_facilities_and_doctors(self, city_id: int, service_id: int) -> dict:
        """Returns {"facilities": [{id, name}], "doctors": [{id, name}]}."""
        data = self._get_json(
            f"{self.DICTIONARY_URL}/facilitiesAndDoctors",
            params={"cityId": city_id, "serviceVariantId": service_id},
        )
        return {
            "facilities": [
                {"id": f["id"], "name": f["name"]} for f in data.get("facilities", [])
            ],
            "doctors": [
                {
                    "id": d["id"],
                    "name": f"{d.get('academicTitle', '')} {d.get('firstName', '')} {d.get('lastName', '')}".strip(),
                }
                for d in data.get("doctors", [])
            ],
        }
