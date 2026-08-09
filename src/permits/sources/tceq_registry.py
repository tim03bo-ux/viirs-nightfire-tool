"""
tceq_registry.py — TCEQ Central Registry enrichment.

The NSR permit search identifies a site only by its RN number: it carries the
*company* name and the permit action, never the site's own name. Central Registry
holds what is missing, one regulated entity at a time:

    Name             WILD HORSE RANCH ENERGY CENTER
    Primary Business POWER GENERATION
    County           KERR
    Nearest City     MOUNTAIN HOME
    Near ZIP         78058

Two things that buys:

  * `primary_business` is TCEQ's own classification, so "is this an electric
    generation site?" stops being a keyword guess and becomes an agency field.
  * the real site name is what makes a TCEQ permit matchable to an ERCOT queue
    entry, which is in turn where megawatts come from — TCEQ never states them.

There is no bulk download and no coordinate field, so this is one HTTP round
trip per RN. Results are cached to a local JSON file; re-running only fetches
RNs it has not seen.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

from ..normalize import clean_str

BASE = "https://www15.tceq.texas.gov/crpub/index.cfm"
USER_AGENT = "viirs-nightfire-tool/permits (+https://github.com/tim03bo-ux/viirs-nightfire-tool)"

DEFAULT_CACHE = os.path.join("data", "cache", "tceq_central_registry.json")

# Primary Business values that mean "this site generates electricity". Matched
# case-insensitively as substrings, since TCEQ appends qualifiers.
GENERATION_BUSINESS = [
    "power generation", "electric power", "electric service", "electricity",
    "cogeneration", "electric generating",
]

# Businesses worth keeping as generation-adjacent load, for colocation work.
LOAD_BUSINESS = [
    "data processing", "data center", "computing", "web hosting",
]

_RE_ID = re.compile(r"re_id=(\d+)")


def _get(url, timeout=60, jar=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = jar or urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor()
    )
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _post(url, fields, timeout=60, jar=None):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = jar or urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor()
    )
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _plain(html):
    text = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text)


def _field(text, label, stop_labels):
    """Pull 'label: value' out of the flattened detail page."""
    start = text.find(label)
    if start < 0:
        return None
    start += len(label)
    end = len(text)
    for stop in stop_labels:
        position = text.find(stop, start)
        if 0 <= position < end:
            end = position
    return clean_str(text[start:end].strip(" :"))


_LABELS = [
    "RN Number:", "Name:", "Primary Business:", "Street Address:", "County:",
    "Nearest City:", "State:", "Near ZIP Code:", "Physical Location:",
    "Affiliated Customers",
]


def lookup(rn_number, timeout=60, jar=None):
    """Fetch one regulated entity. Returns a dict, or None when not found."""
    rn = str(rn_number or "").strip().upper()
    if not rn.startswith("RN"):
        rn = "RN" + re.sub(r"[^0-9]", "", rn)
    if len(rn) < 4:
        return None

    jar = jar or urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    search = _post(
        BASE,
        {
            "fuseaction": "regent.validateRE", "re_ref_num_txt": rn,
            "re_name_txt": "", "addn_num_txt": "", "deliv_txt": "",
            "city_name": "", "zip_cd": "",
        },
        timeout=timeout, jar=jar,
    )
    match = _RE_ID.search(search)
    if not match:
        return None

    detail = _get(
        f"{BASE}?fuseaction=regent.showSingleRe&re_id={match.group(1)}",
        timeout=timeout, jar=jar,
    )
    text = _plain(detail)
    if "RN Number:" not in text:
        return None

    record = {"regulated_entity": rn, "re_id": match.group(1)}
    for label in _LABELS[:-1]:
        key = label.rstrip(":").lower().replace(" ", "_")
        record[key] = _field(text, label, [x for x in _LABELS if x != label])
    return record


def is_generation(primary_business):
    """True when TCEQ's own Primary Business says this site generates power."""
    value = (primary_business or "").lower()
    return any(term in value for term in GENERATION_BUSINESS)


def is_load(primary_business):
    value = (primary_business or "").lower()
    return any(term in value for term in LOAD_BUSINESS)


def load_cache(path=DEFAULT_CACHE):
    if os.path.exists(path):
        with open(path) as handle:
            return json.load(handle)
    return {}


def save_cache(cache, path=DEFAULT_CACHE):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w") as handle:
        json.dump(cache, handle, indent=0, sort_keys=True)


def enrich(rn_numbers, cache_path=DEFAULT_CACHE, delay=0.4, verbose=True,
           limit=None, timeout=60):
    """Look up many RNs, caching as it goes. Returns {rn: record}.

    `delay` throttles between requests — this is one query app serving the whole
    state, and there is no bulk endpoint to fall back on. Interrupting is safe:
    the cache is flushed periodically, so a re-run resumes.
    """
    cache = load_cache(cache_path)
    pending = [
        rn for rn in dict.fromkeys(rn_numbers)
        if rn and str(rn) not in cache
    ]
    if limit:
        pending = pending[:limit]
    if verbose:
        print(f"  {len(cache)} cached, {len(pending)} to fetch")

    jar = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    for index, rn in enumerate(pending, 1):
        try:
            record = lookup(rn, timeout=timeout, jar=jar)
            cache[str(rn)] = record or {"regulated_entity": rn, "not_found": True}
        except Exception as exc:
            # One bad lookup must not lose the batch.
            cache[str(rn)] = {"regulated_entity": rn, "error": str(exc)[:120]}
        if index % 50 == 0:
            save_cache(cache, cache_path)
            if verbose:
                print(f"    {index}/{len(pending)}")
        time.sleep(delay)

    save_cache(cache, cache_path)
    return cache
