"""Shared admin report filters.

One parser and one set of SQL conditions for every admin page and export that can be
narrowed to a campaign (dashboard, patients, issued coupons, redemptions, delivery, CSVs),
so "Campaign A + Service X + date range" means exactly the same rows everywhere and a
filtered page can hand its selection on to an export link unchanged.

Filtering is always on ``PatientOffer.campaign_id`` / ``PatientOffer.offer_id`` — the ids
stored on the registration itself — never on names or on a campaign's current service
list, so history survives a service being removed from a campaign or deactivated.
"""
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from fastapi import Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import Campaign, Offer, Patient, PatientOffer
from ..query_params import optional_date, optional_int
from ..time_utils import clinic_date_range_to_utc

COUPON_STATUSES = ("ACTIVE", "REDEEMED", "EXPIRED", "CANCELLED")
DELIVERY_STATUSES = ("PREPARED", "SENDING", "SENT", "DELIVERED", "FAILED")
DELIVERY_CHANNELS = ("EMAIL", "WHATSAPP")
DATE_FIELDS = {"registered": PatientOffer.created_at, "redeemed": PatientOffer.redeemed_at}
MAX_ID = 2**31 - 1
NO_MATCH_ID = -1


def _id(value: str | None) -> int | None:
    """Blank or non-numeric means "no filter"; a number too large to be a real id is kept
    as a filter that matches nothing rather than being sent to the database.
    """
    number = optional_int(value)
    if number is None:
        return None
    return number if 0 < number <= MAX_ID else NO_MATCH_ID


def _choice(value: str | None, allowed) -> str:
    value = (value or "").strip().upper()
    return value if value in allowed else ""


@dataclass(frozen=True)
class ReportFilters:
    campaign_id: int | None = None
    offer_id: int | None = None
    status: str = ""
    q: str = ""
    start: str = ""
    end: str = ""
    date_field: str = "registered"
    channel: str = ""
    delivery_status: str = ""

    @classmethod
    def from_request(cls, request: Request) -> "ReportFilters":
        params = request.query_params
        start, end = optional_date(params.get("start")), optional_date(params.get("end"))
        date_field = (params.get("date_field") or "").strip().lower()
        return cls(
            campaign_id=_id(params.get("campaign_id")),
            offer_id=_id(params.get("offer_id")),
            status=_choice(params.get("status"), COUPON_STATUSES),
            q=" ".join((params.get("q") or "").split())[:100],
            # A malformed date is dropped (and so is not echoed back into the form).
            start=start.isoformat() if start else "",
            end=end.isoformat() if end else "",
            date_field=date_field if date_field in DATE_FIELDS else "registered",
            channel=_choice(params.get("channel"), DELIVERY_CHANNELS),
            delivery_status=_choice(params.get("delivery_status"), DELIVERY_STATUSES),
        )

    @property
    def start_date(self) -> date | None:
        return date.fromisoformat(self.start) if self.start else None

    @property
    def end_date(self) -> date | None:
        return date.fromisoformat(self.end) if self.end else None

    def query(self, *names: str, **extra) -> str:
        """URL query string carrying the named filters that are set (plus ``extra``), for
        export links and "keep my filters" navigation.
        """
        values = {name: getattr(self, name) for name in names}
        values.update(extra)
        if values.get("date_field") == "registered":
            values.pop("date_field")
        return urlencode({key: value for key, value in values.items() if value not in (None, "")})

    def any(self, *names: str) -> bool:
        return any(getattr(self, name) not in (None, "") for name in names)


def coupon_conditions(filters: ReportFilters, *names: str, date_column=None) -> list:
    """SQL conditions on PatientOffer (and Patient, for ``q``) for the named filters.
    ``dates`` compares against ``date_column`` (default: the registration timestamp) using
    clinic-local calendar days. A query using ``q`` must join Patient.
    """
    conditions = []
    if "campaign_id" in names and filters.campaign_id:
        conditions.append(PatientOffer.campaign_id == filters.campaign_id)
    if "offer_id" in names and filters.offer_id:
        conditions.append(PatientOffer.offer_id == filters.offer_id)
    if "status" in names and filters.status:
        conditions.append(PatientOffer.status == filters.status)
    if "q" in names and filters.q:
        pattern = f"%{filters.q}%"
        conditions.append(or_(Patient.full_name.ilike(pattern), Patient.mobile.ilike(pattern), PatientOffer.coupon_uid.ilike(pattern)))
    if "dates" in names:
        column = date_column if date_column is not None else PatientOffer.created_at
        start_utc, end_utc = clinic_date_range_to_utc(filters.start_date, filters.end_date)
        if start_utc:
            conditions.append(column >= start_utc)
        if end_utc:
            conditions.append(column < end_utc)
    return conditions


def filter_choices(db: Session) -> dict:
    """Dropdown contents: every campaign (closed ones included) and every service
    (inactive ones included), so historical data stays reachable.
    """
    return {
        "campaigns": db.query(Campaign).order_by(Campaign.name).all(),
        "offers": db.query(Offer).order_by(Offer.name).all(),
    }


def filter_summary(db: Session, filters: ReportFilters, *names: str, date_label: str = "Registered") -> dict:
    """Human-readable description of the active filters for page headings, and the
    selected campaign (None for "all campaigns" or an id that matches no campaign).
    """
    parts, campaign = [], None
    if "campaign_id" in names and filters.campaign_id:
        campaign = db.get(Campaign, filters.campaign_id) if filters.campaign_id > 0 else None
        parts.append(f"Campaign: {campaign.name}" if campaign else "Campaign: not found")
    if "offer_id" in names and filters.offer_id:
        offer = db.get(Offer, filters.offer_id) if filters.offer_id > 0 else None
        parts.append(f"Service: {offer.name}" if offer else "Service: not found")
    if "status" in names and filters.status:
        parts.append(f"Status: {filters.status.title()}")
    if "channel" in names and filters.channel:
        parts.append(f"Channel: {filters.channel.title()}")
    if "delivery_status" in names and filters.delivery_status:
        parts.append(f"Delivery status: {filters.delivery_status.title()}")
    if "q" in names and filters.q:
        parts.append(f"Search: “{filters.q}”")
    if "dates" in names and (filters.start or filters.end):
        parts.append(f"{date_label}: {filters.start or 'earliest'} to {filters.end or 'latest'}")
    return {"parts": parts, "campaign": campaign, "campaign_label": campaign.name if campaign else "All campaigns"}


def export_filename(base: str, campaign: Campaign | None) -> str:
    """``base.csv``, or ``base-campaign-<id>-<slug>.csv`` for one campaign. The slug keeps
    only ASCII letters, digits and hyphens, so the header value can never be broken out of.
    """
    if not campaign:
        return f"{base}.csv"
    slug = re.sub(r"[^a-z0-9]+", "-", campaign.name.lower()).strip("-")[:40].strip("-")
    return f"{base}-campaign-{campaign.id}{'-' + slug if slug else ''}.csv"
