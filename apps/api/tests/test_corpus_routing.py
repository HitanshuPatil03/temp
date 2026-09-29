"""Where the harvester files a document, and who it says published it.

These two decisions have to be made together, and the reason this file exists is
that for one harvest they were not. ``route`` re-files each document from its own
filename — CIL's annual-reports page carries every subsidiary's annual report, so
filing by the page they were linked from would defeat a company-wise corpus — but
the organisation name was left behind on the seed. The result was 29 documents in
``ecl/`` stamped "Coal India Limited", in the one file that exists to say where
each figure came from.

The manifest is the committed half of this corpus: the documents are 25 GB and are
not in the repository, so the manifest is what a reader checks the provenance claim
against. A manifest that contradicts its own directory layout is worse than no
manifest, because it looks like a record.

The last class reads that committed manifest and holds it to the same rule.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from mrip.corpus import _ROUTES, SEEDS, _sniff, organisation_of, route
from mrip.normalize.entities import ENTITIES

MANIFEST = Path(__file__).resolve().parents[3] / "data" / "corpus" / "manifest.json"

#: Every company the router can return, with the name that company publishes under.
OFFICIAL = {entity.entity_id: entity.name for entity in ENTITIES}


class TestRouting:
    """A document goes to the company it is about, not the page it was found on."""

    def test_a_subsidiary_report_on_cils_site_is_filed_under_the_subsidiary(self) -> None:
        assert (
            route(
                "https://www.coalindia.in/media/documents/ECL_Annual_Report_2023-24.pdf",
                "Annual Report",
                default="cil",
            )
            == "ecl"
        )

    def test_the_seeds_company_applies_when_nothing_in_the_link_names_one(self) -> None:
        assert (
            route(
                "https://www.coalindia.in/media/documents/Annual_Report.pdf",
                "Annual Report",
                default="cil",
            )
            == "cil"
        )


class TestOrganisation:
    """The publisher name is resolved from where the document was filed."""

    def test_a_rerouted_document_gets_its_new_companys_name(self) -> None:
        """The regression. The seed says CIL; the document is ECL's; ECL wins."""
        url = "https://www.coalindia.in/media/documents/ECL_Annual_Report_2023-24.pdf"
        company = route(url, "Annual Report", default="cil")
        assert organisation_of(company, "Coal India Limited") == (
            "Eastern Coalfields Limited"
        )

    def test_every_company_the_router_can_return_has_a_name_of_its_own(self) -> None:
        """No CIL company may fall through to the seed's name.

        ``organisation_of`` takes a default precisely so one publisher can use it,
        and a default is a silent fallback: if a company were ever dropped from the
        entity table, every document routed to it would inherit whatever seed found
        it and nothing would fail. This is the assertion that would.
        """
        routable = {company for company, _ in _ROUTES}
        fell_through = {
            company
            for company in routable
            if organisation_of(company, "SENTINEL") == "SENTINEL"
        }
        assert fell_through == set()

    def test_the_one_publisher_outside_the_entity_table_keeps_its_seed_name(self) -> None:
        """The Coal Controller is not a coal company, so it is not in ENTITIES."""
        assert organisation_of("coalcontroller", "Coal Controller Organisation") == (
            "Coal Controller Organisation"
        )

    def test_each_seed_agrees_with_the_entity_table_about_its_own_company(self) -> None:
        """Two hand-written name lists that disagree is how the bug got in.

        The seeds carry an organisation name and so does the entity table. Where
        both name the same company they have to say the same thing, or the fallback
        path and the resolved path produce different manifests for one publisher.
        """
        disagreements = [
            (seed.company, seed.organisation, OFFICIAL[seed.company])
            for seed in SEEDS
            if seed.company in OFFICIAL and seed.organisation != OFFICIAL[seed.company]
        ]
        assert disagreements == []


class TestSniff:
    """What a file already on disk is recorded as.

    A re-run does not fetch what it already has, so there is no response header to
    read a media type from. One harvest recorded the *fetch outcome* in that field —
    "already present" — for 2,924 of 3,404 entries.
    """

    def test_a_pdf_is_recognised_by_its_magic_number(self) -> None:
        assert _sniff(b"%PDF-1.4\n%\xc2\xb5", "anything.bin") == "application/pdf"

    def test_the_extension_is_not_trusted_over_the_bytes(self) -> None:
        """A ``.pdf`` that is not a PDF is not recorded as one."""
        assert _sniff(b"<html><body>404", "report.pdf") == "application/octet-stream"

    def test_the_two_ooxml_formats_are_told_apart_by_extension(self) -> None:
        """Both are zips with the same four bytes; only the name distinguishes them."""
        zip_magic = b"PK\x03\x04\x14\x00\x06\x00"
        assert _sniff(zip_magic, "offtake.xlsx").endswith("spreadsheetml.sheet")
        assert _sniff(zip_magic, "note.docx").endswith("wordprocessingml.document")

    def test_nothing_recognisable_is_never_guessed_at(self) -> None:
        assert _sniff(b"\x00\x01\x02\x03", "mystery.pdf") == "application/octet-stream"


class TestCommittedManifest:
    """The provenance record in the repository, held to the rule above.

    This runs on a fresh clone: the manifest is committed even though the documents
    it points at are not.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def documents(cls) -> list[dict[str, object]]:
        if not MANIFEST.exists():
            pytest.skip(f"no corpus manifest at {MANIFEST}")
        with MANIFEST.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        documents: list[dict[str, object]] = manifest["documents"]
        return documents

    def test_one_company_never_carries_two_publisher_names(
        self, documents: list[dict[str, object]]
    ) -> None:
        names: defaultdict[object, set[object]] = defaultdict(set)
        for document in documents:
            names[document["company"]].add(document["organisation"])
        mixed = {
            company: sorted(map(str, found))
            for company, found in names.items()
            if len(found) > 1
        }
        assert mixed == {}

    def test_every_publisher_name_is_the_one_that_company_publishes_under(
        self, documents: list[dict[str, object]]
    ) -> None:
        wrong = [
            (company, document["organisation"], OFFICIAL[company])
            for document in documents
            if (company := str(document["company"])) in OFFICIAL
            and document["organisation"] != OFFICIAL[company]
        ]
        assert wrong == []

    def test_the_count_matches_the_documents_listed(
        self, documents: list[dict[str, object]]
    ) -> None:
        with MANIFEST.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        assert manifest["count"] == len(documents)
        assert sum(manifest["companies"].values()) == len(documents)

    def test_every_document_can_be_traced_back_to_a_source(
        self, documents: list[dict[str, object]]
    ) -> None:
        """A manifest entry with no URL or no hash is not a provenance record."""
        incomplete = [
            document["filename"]
            for document in documents
            if not document.get("url") or not document.get("content_hash")
        ]
        assert incomplete == []

    def test_every_entry_records_a_media_type_and_not_a_fetch_outcome(
        self, documents: list[dict[str, object]]
    ) -> None:
        """The field says what the file *is*, not how the harvester came by it."""
        not_a_type = sorted(
            {
                str(document["content_type"])
                for document in documents
                if "/" not in str(document["content_type"])
            }
        )
        assert not_a_type == []

    def test_no_url_was_fetched_twice(self, documents: list[dict[str, object]]) -> None:
        """Two entries for one address mean the harvester downloaded it twice.

        Identical *bytes* under two entries is expected — a subsidiary's annual
        report is published on its own site and on ``coalindia.in`` — but each
        entry has to be a different address, or the manifest is counting a single
        download as two documents.
        """
        seen: Counter[object] = Counter(document["url"] for document in documents)
        assert [url for url, times in seen.items() if times > 1] == []

    def test_a_file_under_two_companies_is_the_same_file(
        self, documents: list[dict[str, object]]
    ) -> None:
        """The one way the router is allowed to disagree with itself.

        ``coalindia.in`` republishes subsidiary reports at paths that name no
        subsidiary, so the same bytes can route to the subsidiary from its own
        site and to ``cil`` from the group's. That is a known limit of routing by
        URL. What must never happen is two *different* documents sharing a hash,
        which would mean the provenance record cannot tell them apart.
        """
        sizes: defaultdict[object, set[object]] = defaultdict(set)
        for document in documents:
            sizes[document["content_hash"]].add(document["size_bytes"])
        disagreeing = {
            str(content_hash): sorted(map(str, found))
            for content_hash, found in sizes.items()
            if len(found) > 1
        }
        assert disagreeing == {}
