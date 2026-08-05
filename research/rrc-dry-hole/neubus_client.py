#!/usr/bin/env python3
"""Client for the RRC imaged-records archive on Neubus (rrcsearch3.neubus.com).

The RRC's "Imaged Records" query apps (Wildcat & Suspense, Oil & Gas Well
Records, etc.) are a Vue single-page app that talks to a JSON backend. Public
access is anonymous: the page mints its OWN short-lived self-signed JWT for a
built-in "publicuser" -- there is no login and no shared secret. This module
reproduces that token and drives the same API the browser uses, so rolls and
individual microfilm frames can be pulled headlessly.

Profiles (the `profile_id` below):
    17  Imaged Oil and Gas Well Records (indexed: operator/lease/county/API/field)
    84  Wildcat & Suspense (indexed only by reel_number + document_type)

Flow for a Wildcat & Suspense roll:
    search profile 84 by reel_number -> doc_id
      -> getViewRecordOauth(doc_id)        -> record tabs (Document / Attachment)
      -> getTabFoldersFiles(tab doc_id)    -> files, each with a `nuid`
      -> GET {FS_URL}/single/{nuid}?profileId=..   -> the PDF or TIF bytes

The "Document" tab holds the whole roll as one PDF; the "Attachment" tab holds
individual scanned frames as TIFs.

Requires: requests, PyJWT, cryptography  (pip install requests PyJWT cryptography)

Examples:
    # what fields can a profile be searched on?
    python3 neubus_client.py fields --profile 84

    # find a Wildcat & Suspense roll and list its files
    python3 neubus_client.py roll --reel WS7C-2

    # download the whole-roll PDF
    python3 neubus_client.py roll --reel WS7C-2 --download-pdf WS7C-2.pdf

    # search the indexed Oil & Gas records
    python3 neubus_client.py search --profile 17 \
        --field county=Upton --field operator_name=KIMBRELL
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import requests

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:  # pragma: no cover
    sys.exit("Need PyJWT and cryptography: pip install PyJWT cryptography")

BASE = "https://rrcsearch3.neubus.com"
FS = "https://rrcsearch3fs.neubus.com/api/v1"
REALM = "rrcsearch3"  # derived from the hostname, per the app's own logic
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# The fixed public-user identity the SPA signs its anonymous token for.
PUBLIC_CLAIMS = {
    "email": "publicuser@neubus.com",
    "family_name": "User",
    "given_name": "Public",
    "name": "Public User",
    "preferred_username": "publicuser",
    "sub": "f7ec6582-3d68-4eae-b6e5-39fcb321ed4c",
}


def mint_public_token() -> str:
    """Sign the same anonymous public-user JWT the browser generates.

    A fresh random RSA key each call, exactly like the SPA -- the backend only
    checks that the token is a well-formed public token, not its signer.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {
        **PUBLIC_CLAIMS,
        "iss": f"https://ndeiam.neubus.com/realms/{REALM}",
        "aud": "public",
        "iat": now,
        "exp": now + 86400,
    }
    return jwt.encode(key=key, payload=claims, algorithm="PS512",
                      headers={"typ": "JWT"})


class Neubus:
    def __init__(self, profile_id: int):
        self.profile_id = profile_id
        self.token = mint_public_token()
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        page = self.s.get(f"{BASE}/search-profile?profileId={profile_id}", timeout=60)
        m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
        if not m:
            raise RuntimeError("could not read CSRF token from the search page")
        self.s.headers.update({
            "X-CSRF-TOKEN": m.group(1),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/search-profile?profileId={profile_id}",
            "Authorization": f"Bearer {self.token}",
        })

    def _post(self, ep: str, payload) -> dict:
        r = self.s.post(BASE + ep, json=payload, timeout=180)
        r.raise_for_status()
        return r.json()

    def _get(self, ep: str, params) -> dict:
        r = self.s.get(BASE + ep, params=params, timeout=180)
        r.raise_for_status()
        return r.json()

    def fields(self) -> list[dict]:
        """Return the profile's searchable field controls."""
        j = self._post("/getControls",
                       {"is_bulk_indexing": False, "profile_id": self.profile_id})
        out = []
        for ctl in j["data"]["controls"]["controls"]:
            for name, v in ctl.items():
                if v.get("search_display"):
                    out.append({"field": name, "label": v.get("label"),
                                "type": v.get("type"),
                                "wildcard": v.get("allows_wildcard")})
        return out

    def search(self, items: dict, page_size: int = 50) -> list[dict]:
        """Search the profile. `items` maps field_name -> value."""
        neusearch = {
            "excludeName": "", "excludeValue": "", "extraParams": "",
            "includeName": "", "order": "asc", "orderBy": "", "page": 1,
            "pageSize": page_size, "profile": self.profile_id,
            "recordFromDate": "", "recordToDate": "", "saveSearch": "true",
            "Searchitems": {"item": [{"key": k, "value": v} for k, v in items.items()]},
            "strict": "false",
        }
        j = self._post("/getSearchImages", neusearch)
        data = j.get("data", {}).get("data")
        if not isinstance(data, dict):
            return []
        images = (data.get("search_results") or {}).get("images") or []
        return [{f["field_name"]: f["field_value"] for f in im.get("image_fields", [])}
                | {"doc_id": im["doc_id"]} for im in images]

    def record_files(self, doc_id: str) -> dict[str, list[dict]]:
        """For a search-result doc_id, return {tab_description: [files...]}.

        Each file dict carries a `nuid` used to fetch the bytes. NOTE: a doc_id
        is bound to the session/token that produced it -- use it in the same
        Neubus instance, don't persist it.
        """
        vr = self._post("/getViewRecordOauth",
                        {"doc_id": doc_id, "profile_id": self.profile_id})
        out: dict[str, list[dict]] = {}
        for _tab, items in vr["data"]["data"].items():
            for it in items:
                ff = self._get("/getTabFoldersFiles",
                               {"profile_id": self.profile_id, "doc_id": it["doc_id"]})
                out[it["description"]] = ff["data"]["data"]["files"]
        return out

    def download(self, nuid: str, dest: str) -> int:
        """Stream a file (PDF/TIF) by nuid to `dest`. Returns bytes written."""
        url = f"{FS}/single/{nuid}"
        with self.s.get(url, params={"profileId": self.profile_id},
                        headers={"Authorization": f"Bearer {self.token}"},
                        stream=True, timeout=900) as r:
            r.raise_for_status()
            total = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
                    total += len(chunk)
        return total


def cmd_fields(args):
    for f in Neubus(args.profile).fields():
        print(f"  {f['field']:<22} {str(f['label']):<28} "
              f"type={f['type']} wildcard={f['wildcard']}")


def cmd_search(args):
    items = dict(kv.split("=", 1) for kv in args.field)
    rows = Neubus(args.profile).search(items, page_size=args.limit)
    print(f"{len(rows)} result(s):")
    for r in rows:
        keys = ["operator_name", "lease_name", "county", "api_ft", "field_name",
                "document_type", "reel_number"]
        print("  ", {k: r[k] for k in keys if r.get(k)})


def cmd_roll(args):
    nb = Neubus(84)
    rows = nb.search({"reel_number": args.reel})
    if not rows:
        sys.exit(f"no Wildcat & Suspense record for reel {args.reel!r}")
    doc_id = rows[0]["doc_id"]
    tabs = nb.record_files(doc_id)
    for tab, files in tabs.items():
        print(f"\n{tab}: {len(files)} file(s)")
        for f in sorted(files, key=lambda x: x["name"])[:6]:
            print(f"   {f['name']:<20} {f['file_size']:>12,} bytes  nuid={f['nuid']}")
        if len(files) > 6:
            print(f"   ... and {len(files) - 6} more")
    if args.download_pdf:
        pdf = next((f for f in tabs.get("Document", []) if f["name"].endswith(".pdf")), None)
        if not pdf:
            sys.exit("no PDF found in the Document tab")
        print(f"\nDownloading {pdf['name']} -> {args.download_pdf}")
        n = nb.download(pdf["nuid"], args.download_pdf)
        print(f"  wrote {n:,} bytes")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fields", help="list a profile's searchable fields")
    p.add_argument("--profile", type=int, default=84)
    p.set_defaults(func=cmd_fields)

    p = sub.add_parser("search", help="search a profile by indexed fields")
    p.add_argument("--profile", type=int, default=17)
    p.add_argument("--field", action="append", default=[], metavar="name=value",
                   help="repeatable, e.g. --field county=Upton")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("roll", help="open a Wildcat & Suspense roll by reel number")
    p.add_argument("--reel", required=True, help="e.g. WS7C-2 (exactly as in the index)")
    p.add_argument("--download-pdf", metavar="PATH", help="save the whole-roll PDF")
    p.set_defaults(func=cmd_roll)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
