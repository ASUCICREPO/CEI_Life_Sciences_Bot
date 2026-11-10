"""Bounded NCBI target primitives.

Callers must supply a shared, durable rate limiter before deploying these functions.
The limiter must raise when a request cannot be made within NCBI's limits.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
BLAST = "https://blast.ncbi.nlm.nih.gov/Blast.cgi"
MAX_RESPONSE_BYTES = 1_000_000
_ACCESSION = re.compile(r"^[A-Za-z0-9_]{1,40}(?:\.[0-9]{1,4})?$")
_RID = re.compile(r"^[A-Za-z0-9-]{5,40}$")
_TAXON_NAME = re.compile(r"^[A-Za-z0-9 ._()'\-]{1,120}$")
_SEQUENCE = re.compile(r"^[A-Za-z*\-\r\n ]{1,10000}$")
RateLimit = Callable[[str, str | None], None]


def _request(url: str, params: dict[str, str], *, post: bool = False) -> str:
    encoded = urlencode(params).encode()
    req = Request(url if post else f"{url}?{encoded.decode()}", data=encoded if post else None,
                  headers={"User-Agent": "CEI-LifeSciences-Bot/1.0"})
    with urlopen(req, timeout=20) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("NCBI response exceeds size limit")
    return data.decode("utf-8")


def _safe_xml(text: str) -> ElementTree.Element:
    # NCBI supplies an external DTD declaration. Remove only a simple declaration;
    # reject internal subsets and entity definitions before using ElementTree.
    if "<!ENTITY" in text.upper():
        raise ValueError("unsupported XML entity")
    text = re.sub(r'<!DOCTYPE\s+[A-Za-z0-9_:-]+\s+(?:PUBLIC\s+"[^"<>\[\]]*"\s+"[^"<>\[\]]*"|SYSTEM\s+"[^"<>\[\]]*")\s*>',
                  "", text, flags=re.IGNORECASE)
    if "<!DOCTYPE" in text.upper():
        raise ValueError("unsupported XML declaration")
    return ElementTree.fromstring(text)


def taxonomy_lineage(taxon: str, *, acquire: RateLimit, request: Callable[..., str] = _request) -> dict[str, Any]:
    if not isinstance(taxon, str) or not _TAXON_NAME.fullmatch(taxon.strip()):
        raise ValueError("invalid taxon")
    taxon = taxon.strip()
    taxid = taxon
    if not taxon.isdigit():
        acquire("eutils", None)
        response = json.loads(request(ESEARCH, {"db": "taxonomy", "term": taxon, "retmode": "json", "retmax": "2"}))
        ids = response.get("esearchresult", {}).get("idlist", [])
        if not isinstance(ids, list) or len(ids) != 1 or not str(ids[0]).isdigit():
            raise ValueError("taxonomy name does not identify exactly one taxon")
        taxid = str(ids[0])
    acquire("eutils", None)
    root = _safe_xml(request(EFETCH, {"db": "taxonomy", "id": taxid, "retmode": "xml"}))
    node = root.find("./Taxon")
    if node is None or node.findtext("TaxId") != taxid:
        raise ValueError("taxonomy record not found")
    lineage = [{"tax_id": item.findtext("TaxId"), "name": item.findtext("ScientificName"),
                "rank": item.findtext("Rank")} for item in node.findall("./LineageEx/Taxon")]
    return {"tax_id": taxid, "name": node.findtext("ScientificName"), "rank": node.findtext("Rank"),
            "lineage": lineage, "url": f"https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id={taxid}"}


def sequence_efetch(accession: str, *, database: str, format: str, acquire: RateLimit,
                    request: Callable[..., str] = _request) -> dict[str, Any]:
    if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession):
        raise ValueError("invalid sequence accession")
    if database not in {"nuccore", "protein"} or format not in {"fasta", "gb"}:
        raise ValueError("invalid sequence database or format")
    acquire("eutils", None)
    rettype = "gp" if database == "protein" and format == "gb" else format
    record = request(EFETCH, {"db": database, "id": accession, "rettype": rettype, "retmode": "text"})
    if (not record.strip() or (format == "fasta" and not record.lstrip().startswith(">"))
            or (format == "gb" and (not record.lstrip().startswith("LOCUS ") or not record.rstrip().endswith("//")))):
        raise ValueError("sequence record not found")
    return {"accession": accession, "database": database, "format": format, "record": record,
            "url": f"https://www.ncbi.nlm.nih.gov/{'nuccore' if database == 'nuccore' else 'protein'}/{accession}"}


def blast_submit(sequence: str, *, program: str, database: str, acquire: RateLimit,
                 contact_email: str, request: Callable[..., str] = _request) -> dict[str, Any]:
    if not isinstance(sequence, str) or not _SEQUENCE.fullmatch(sequence):
        raise ValueError("invalid BLAST sequence")
    if program not in {"blastn", "blastp", "blastx", "tblastn", "tblastx"}:
        raise ValueError("invalid BLAST program")
    if database not in {"core_nt", "nt", "refseq_rna", "nr", "swissprot", "refseq_protein"}:
        raise ValueError("invalid BLAST database")
    if not isinstance(contact_email, str) or not re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}", contact_email):
        raise ValueError("NCBI contact email is required")
    acquire("blast", None)
    response = request(BLAST, {"CMD": "Put", "PROGRAM": program, "DATABASE": database,
                               "QUERY": sequence, "tool": "cei-life-sciences", "email": contact_email}, post=True)
    rid = re.search(r"^\s*RID\s*=\s*([A-Za-z0-9-]+)\s*$", response, re.MULTILINE)
    rtoe = re.search(r"^\s*RTOE\s*=\s*(\d+)\s*$", response, re.MULTILINE)
    if not rid or not _RID.fullmatch(rid.group(1)):
        raise ValueError("BLAST did not return a request identifier")
    return {"rid": rid.group(1), "estimated_seconds": int(rtoe.group(1)) if rtoe else None,
            "poll_after_seconds": max(60, int(rtoe.group(1)) if rtoe else 0)}


def blast_poll(rid: str, *, acquire: RateLimit, contact_email: str,
               request: Callable[..., str] = _request) -> dict[str, Any]:
    if not isinstance(rid, str) or not _RID.fullmatch(rid):
        raise ValueError("invalid BLAST request identifier")
    if not isinstance(contact_email, str) or not re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}", contact_email):
        raise ValueError("NCBI contact email is required")
    # The shared limiter must enforce both >=10s globally and >=60s for this RID.
    acquire("blast", rid)
    response = request(BLAST, {"CMD": "Get", "RID": rid, "FORMAT_TYPE": "XML",
                               "tool": "cei-life-sciences", "email": contact_email})
    status = re.search(r"^\s*Status=(WAITING|FAILED|UNKNOWN)\s*$", response, re.MULTILINE)
    if status:
        return {"rid": rid, "status": status.group(1).lower(), "poll_after_seconds": 60}
    if not response.lstrip().startswith("<?xml") and not response.lstrip().startswith("<BlastOutput"):
        raise ValueError("invalid BLAST response")
    # BLAST XML can include a standard external DTD declaration. It is returned as
    # data and must never be parsed using an external-entity-capable parser.
    return {"rid": rid, "status": "ready", "xml": response,
            "url": f"https://blast.ncbi.nlm.nih.gov/Blast.cgi?CMD=Get&RID={rid}"}
