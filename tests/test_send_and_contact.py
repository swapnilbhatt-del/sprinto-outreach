"""Send refuses any recipient other than OWN_EMAIL; Hunter cache + quota; contact ranking; quote validation."""
import json

import pytest

from src.poc_hunter import choose_contact, domain_search
from src.research.store import Store
from src.research.validate import Source, SourceItem, validate
from src.send_gmail import RecipientNotAllowed, send
from src.shortlist import shortlist

from .conftest import TODAY, unknown_extraction

OWN = "me@example.com"


class FakeGmail:
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        self.sent.append(body)
        return self

    def execute(self):
        return {"id": "msg-1"}


@pytest.mark.parametrize("to", ["prospect@acme.com", "ME@example.com.evil.com", "", "me@example.org"])
def test_send_refuses_other_recipients(to):
    gmail = FakeGmail()
    with pytest.raises(RecipientNotAllowed):
        send(to, "s", "b", OWN, service=gmail)
    with pytest.raises(RecipientNotAllowed):
        send(to, "s", "b", OWN, dry_run=True)
    assert gmail.sent == []


def test_send_refuses_when_own_email_unset():
    with pytest.raises(RecipientNotAllowed):
        send("anyone@acme.com", "s", "b", None, dry_run=True)


def test_send_to_own_email_works_and_dry_run_does_not_send():
    gmail = FakeGmail()
    out = []
    assert send(OWN, "s", "b", OWN, dry_run=True, service=gmail, print_fn=out.append) == ("dry_run", None)
    assert gmail.sent == [] and "[dry-run] To: me@example.com" in out[0]
    assert send(OWN, "s", "b", OWN, service=gmail) == ("sent", "msg-1")
    assert len(gmail.sent) == 1


def test_review_declined_does_not_send():
    gmail = FakeGmail()
    mode, _ = send(OWN, "s", "b", OWN, review=True, service=gmail, input_fn=lambda _: "n", print_fn=lambda _: None)
    assert mode == "declined" and gmail.sent == []


class FakeHTTP:
    def __init__(self, body, filtered_body=None):
        self.body, self.filtered_body, self.calls, self.params = body, filtered_body, 0, []

    def get(self, url, params):
        self.calls += 1
        self.params.append(dict(params))
        body = self.filtered_body if (self.filtered_body is not None and "department" in params) else self.body

        class R:
            status_code = 200
            text = json.dumps(body)
            headers = {"X-Credits-Used": "0.5"}

            def json(self):
                return body
        return R()


HUNTER = {"data": {"emails": [
    {"value": "cto@acme.com", "first_name": "Ana", "last_name": "Rao", "position": "CTO",
     "confidence": 99, "verification": {"status": "valid"}},
    {"value": "ciso@acme.com", "first_name": "Ben", "last_name": "Li", "position": "CISO",
     "confidence": 90, "verification": {"status": "accept_all"}},
    {"value": "vpe@acme.com", "first_name": "Cy", "last_name": "Do", "position": "VP of Engineering",
     "confidence": 80, "verification": {"status": "valid"}},
    {"value": "sales@acme.com", "first_name": "Di", "position": "Sales Director",
     "confidence": 95, "verification": {"status": "valid"}},
]}}


def test_hunter_cached_domain_is_never_searched_twice(tmp_path):
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP(HUNTER)
    assert domain_search(store, "acme.com", "k", 25, http=http, today=TODAY)[1] == "api"
    assert domain_search(store, "acme.com", "k", 25, http=http, today=TODAY)[1] == "cache"
    assert http.calls == 1 and store.hunter_used("2026-11") == 1


def test_hunter_refuses_when_monthly_quota_reached(tmp_path):
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP(HUNTER)
    for _ in range(25):
        store.hunter_increment("2026-11")
    assert domain_search(store, "new.com", "k", 25, http=http, today=TODAY) == (None, "quota_exhausted")
    assert http.calls == 0


def test_contact_ranking_skips_unverified_and_records_why(icp):
    c = icp["contact"]
    contact, why, considered = choose_contact(HUNTER, c["title_ranking"], c["title_aliases"],
                                              c["accepted_verification_status"])
    assert contact["email"] == "vpe@acme.com"          # CISO rejected (accept_all), VP Eng outranks CTO
    assert "VP Engineering" in why
    assert any(r["email"] == "ciso@acme.com" and "accept_all" in r["rejected"] for r in considered)
    assert any(r["email"] == "sales@acme.com" and r["rejected"] == "title not in ranked list" for r in considered)


def test_validate_drops_facts_without_supporting_quote():
    raw = unknown_extraction()
    raw["is_software_product"] = {"value": "yes", "quote": "The compliance platform for SaaS", "item": "none"}
    raw["employee_count"] = {"value": "500 people", "min": "500", "max": "500", "quote": "we are 500 people",
                             "item": "none", "date": "unknown"}
    src = Source("https://acme.com/", {"none": SourceItem("https://acme.com/", "Acme. The compliance platform for SaaS.",
                                                          "page", "2026-11-04")})
    facts, dropped = validate(raw, src)
    assert [f["key"] for f in facts] == ["is_software_product"]
    assert dropped[0]["key"] == "employee_count" and "quote" in dropped[0]["reason"]


def test_shortlist_removes_crm_customers_and_caps(icp, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "hubspot_customers.csv").write_text(
        "Record ID,Company name,Company Domain Name\n1,Chargebee,https://www.Chargebee.com/\n")
    icp["run"]["max_companies_per_run"] = 2
    kept, removed = shortlist(["chargebee.com", "a.com", "WWW.b.com", "c.com", "a.com"], icp, root=tmp_path)
    assert kept == ["a.com", "b.com"]
    assert {r["domain"]: r["reason"].split()[0] for r in removed} == {"chargebee.com": "in", "c.com": "over"}



FILTERS = {"department": "it,executive,management", "required_field": "position"}


def test_hunter_sends_department_filter_and_caches(tmp_path):
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP(HUNTER)
    body, how = domain_search(store, "acme.com", "k", 25, http=http, today=TODAY, filters=FILTERS)
    assert how == "api" and http.calls == 1
    assert http.params[0]["department"] == "it,executive,management"
    assert body["meta"]["filters_used"] == [FILTERS]
    assert domain_search(store, "acme.com", "k", 25, http=http, today=TODAY, filters=FILTERS)[1] == "cache"
    assert http.calls == 1 and store.hunter_used("2026-11") == 1


def test_hunter_falls_back_once_when_filter_finds_nobody(tmp_path):
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP(HUNTER, filtered_body={"data": {"emails": []}})
    body, how = domain_search(store, "acme.com", "k", 25, http=http, today=TODAY, filters=FILTERS)
    assert how == "api_unfiltered" and http.calls == 2 and "department" not in http.params[1]
    assert body["data"]["emails"] and store.hunter_used("2026-11") == 2


def test_hunter_fallback_respects_quota(tmp_path):
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP(HUNTER, filtered_body={"data": {"emails": []}})
    for _ in range(24):
        store.hunter_increment("2026-11")
    body, how = domain_search(store, "acme.com", "k", 25, http=http, today=TODAY, filters=FILTERS)
    assert http.calls == 1 and store.hunter_used("2026-11") == 25     # no second call past the cap
    assert how == "api" and body["data"]["emails"] == []



def test_hunter_widens_when_filtered_result_has_no_usable_contact(tmp_path, icp):
    c = icp["contact"]
    junk = {"data": {"emails": [{"value": "x@acme.com", "position": "Eventila Technology",
                                 "verification": {"status": None}, "confidence": 75}]}}
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP(HUNTER, filtered_body=junk)
    usable = lambda b: choose_contact(b, c["title_ranking"], c["title_aliases"], c["accepted_verification_status"])[0] is not None
    body, how = domain_search(store, "acme.com", "k", 25, http=http, today=TODAY, filters=FILTERS, usable=usable)
    assert how == "api_unfiltered" and http.calls == 2 and store.hunter_used("2026-11") == 2
    assert body["data"]["emails"][0]["value"] == "x@acme.com"          # filtered results kept, then merged
    assert usable(body)
    assert domain_search(store, "acme.com", "k", 25, http=http, today=TODAY, filters=FILTERS, usable=usable)[1] == "cache"
    assert http.calls == 2


from src.poc_hunter import company_headcount, parse_employee_range

COMPANY = {"data": {"name": "Acme", "indexedAt": "2026-11-02T10:00:00Z",
                    "metrics": {"employees": "251-1K", "employeesCount": 515}}}


def test_parse_hunter_size_buckets():
    assert parse_employee_range("51-200") == (51, 200)
    assert parse_employee_range("251-1K") == (251, 1000)
    assert parse_employee_range("1K-5K") == (1000, 5000)
    assert parse_employee_range("10K+")[0] == 10000
    assert parse_employee_range("lots") is None and parse_employee_range(None) is None


def test_company_headcount_is_cached_and_free(tmp_path):
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP(COMPANY)
    facts, how = company_headcount(store, "acme.com", "k", 25, http=http, today=TODAY)
    assert how == "ok" and facts[0]["value"] == {"value": "251-1K", "min": 251, "max": 1000}
    assert facts[0]["date"] == "2026-11-02" and "api_key" not in facts[0]["source_url"]
    assert company_headcount(store, "acme.com", "k", 25, http=http, today=TODAY)[1] == "cache"
    assert http.calls == 1 and store.hunter_credits("2026-11") == 0      # Hunter bills company lookups 0


def test_company_headcount_still_runs_when_paid_credits_are_spent(tmp_path):
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP(COMPANY)
    for _ in range(25):
        store.hunter_increment("2026-11")                      # 25 searches = 25 credits
    assert company_headcount(store, "acme.com", "k", 25, http=http, today=TODAY)[1] == "ok"   # free call
    assert company_headcount(store, "new.com", None, 25, http=http, today=TODAY) == ([], "no_key")
    assert http.calls == 1


def test_company_headcount_future_indexed_date_is_treated_as_today(tmp_path):
    body = json.loads(json.dumps(COMPANY))
    body["data"]["indexedAt"] = "2026-12-30T00:00:00Z"
    store = Store(tmp_path / "t.sqlite")
    facts, _ = company_headcount(store, "acme.com", "k", 25, http=FakeHTTP(body), today=TODAY)
    assert facts[0]["date"] == TODAY.isoformat()
    assert "future date" in facts[0]["quote"] and "2026-12-30" in facts[0]["quote"]


def test_gmail_client_file_check_never_needs_a_web_client(tmp_path):
    from src.gmail_test import check_client_file
    assert check_client_file(None)[0] is False
    assert check_client_file(str(tmp_path / "missing.json"))[0] is False
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    ok, msg = check_client_file(str(web))
    assert not ok and "Desktop app" in msg and "y" not in msg.split("'")[0][-3:]
    desk = tmp_path / "desk.json"
    desk.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "s3cr3t"}}))
    ok, msg = check_client_file(str(desk))
    assert ok and "s3cr3t" not in msg



def test_email_verifier_is_cached_and_budgeted(tmp_path):
    from src.poc_hunter import verify_email
    store, http = Store(tmp_path / "t.sqlite"), FakeHTTP({"data": {"status": "valid"}})
    assert verify_email(store, "CEO@acme.com", "k", 25, http=http, today=TODAY) == ("valid", "api")
    assert verify_email(store, "ceo@acme.com", "k", 25, http=http, today=TODAY) == ("valid", "cache")
    assert http.calls == 1 and store.hunter_credits("2026-11") == 0.5
    for _ in range(25):
        store.hunter_increment("2026-11")
    assert verify_email(store, "new@acme.com", "k", 25, http=http, today=TODAY) == (None, "quota_exhausted")


def test_unverified_ceo_becomes_contact_after_verifier_says_valid(icp):
    from src.poc_hunter import verify_unverified_candidates
    c = icp["contact"]
    body = {"data": {"emails": [
        {"value": "rm@w.in", "first_name": "Pa", "position": "Relationship Manager", "verification": {"status": "valid"}},
        {"value": "ceo@w.in", "first_name": "Di", "position": "CEO", "verification": None, "confidence": 90},
        {"value": "fo@w.in", "first_name": "Fo", "position": "Founder's Office Associate", "verification": None}]}}
    assert choose_contact(body, c["title_ranking"], c["title_aliases"], c["accepted_verification_status"])[0] is None
    checked = []
    body2, log = verify_unverified_candidates(body, c["title_ranking"], c["title_aliases"],
                                              lambda e: (checked.append(e), ("valid", "api"))[1], 2)
    assert checked == ["ceo@w.in"]                       # founder's-office title is not a ranked title
    contact, why, _ = choose_contact(body2, c["title_ranking"], c["title_aliases"], c["accepted_verification_status"])
    assert contact["email"] == "ceo@w.in" and contact["matched_title"] == "CEO"
    assert body["data"]["emails"][1]["verification"] is None    # the cached Hunter body is not mutated


def test_funding_round_must_be_named_in_quote():
    from src.research.validate import round_named
    assert not round_named("series_d_plus", "Auto AI firm Intangles logs $30 million led by Avataar")
    assert round_named("series_b", "raised $30M in a Series-B round") and round_named("series_c", "its Series C")
    assert not round_named("series_a", "Series AI platform")
    assert round_named("acquired", "was acquired by Acme") and round_named("public", "after its IPO")


def test_name_order_warning_flags_possible_swap_only():
    from src.poc_hunter import name_order_warning
    assert "swapped" in name_order_warning("Rao", "Meera", "meera@w.in")
    assert name_order_warning("Meera", "Rao", "meera@w.in") is None
    assert name_order_warning("Ana", "Rao", "ana.rao@acme.com") is None
    assert name_order_warning("Ana", None, "rao@acme.com") is None


def test_shortlist_excludes_only_customer_stage_when_stage_column_given(icp, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "hubspot_customers.csv").write_text(
        "Company name,Company Domain Name,Lifecycle Stage\nCust,cust.com,Customer\nLeadCo,lead.com,lead\nBlank,blank.com,\n")
    spec = icp["run"]["shortlist_exclusions"][0]
    spec.update(stage_column="Lifecycle Stage", exclude_stages=["customer"])
    icp["run"]["shortlist_exclusions"] = [spec]
    kept, removed = shortlist(["cust.com", "lead.com", "blank.com"], icp, root=tmp_path)
    assert kept == ["lead.com"]
    assert sorted(r["domain"] for r in removed) == ["blank.com", "cust.com"]



def test_effective_quota_never_exceeds_hunters_live_balance(tmp_path):
    from src.poc_hunter import effective_quota
    store = Store(tmp_path / "t.sqlite")
    for _ in range(10):
        store.hunter_increment("2026-11")
    assert effective_quota(store, 50, 39.5, today=TODAY) == 49.5     # 10 used here + 39.5 Hunter says remain
    assert effective_quota(store, 50, 2, today=TODAY) == 12          # Hunter knows of other usage: trust it
    assert effective_quota(store, 50, None, today=TODAY) == 50       # account call failed: local cap alone


def test_email_finder_is_cached_and_costs_one_credit(tmp_path):
    from src.poc_hunter import find_email
    store = Store(tmp_path / "t.sqlite")
    http = FakeHTTP({"data": {"email": "ab@acme.com", "score": 95, "position": "CEO",
                              "verification": {"status": "valid"}}})
    data, how = find_email(store, "acme.com", "Ab", "Cd", "k", 50, http=http, today=TODAY)
    assert how == "api" and data["email"] == "ab@acme.com"
    assert find_email(store, "acme.com", "ab", "cd", "k", 50, http=http, today=TODAY)[1] == "cache"
    assert http.calls == 1 and store.hunter_credits("2026-11") == 1
    assert find_email(store, "x.com", "A", "B", "k", 1, http=http, today=TODAY) == (None, "quota_exhausted")
