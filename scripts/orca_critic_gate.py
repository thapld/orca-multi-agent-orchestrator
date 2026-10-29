#!/usr/bin/env python3
"""
Shared pure-Python module (stdlib only, no DB access) for Orca Critic Consensus Gate.
Provides canonical plan hashing, item/contract extraction, automated risk scoring,
structured review validation, independence verification, and consensus file validation.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any

CANON_VERSION = "orca.critic.review/v1"
CONSENSUS_SCHEMA = "orca.critic.consensus/v1"
HYPOTHESIS_SCHEMA = "orca.critic.hypothesis/v1"

REQUIRED_AXES = (
    "disjointOwnership",
    "appVsInfra",
    "compositeKeyIsolation",
    "dbApiCompat",
    "qaThreeLayers",
)
VALID_AXIS_VALUES = {"pass", "warn", "fail"}
VALID_VERDICTS = {"CONSENSUS_REACHED", "CHANGES_REQUIRED", "DEADLOCK"}
VALID_SEVERITIES = {"blocker", "major", "minor"}
VALID_RESOLUTION_STATUSES = {"resolved", "accepted_risk", "unresolved"}

PATH_LINE_RE = re.compile(r"^[A-Za-z0-9_./\-]+:\d+(?:-\d+)?$")


def canonical_plan_bytes(text: str) -> bytes:
    """NFC -> LF -> strip trailing spaces per line -> drop <!-- plan-hash: --> lines -> strip."""
    norm = unicodedata.normalize("NFC", text or "")
    norm = norm.replace("\r\n", "\n").replace("\r", "\n")
    out_lines: list[str] = []
    for raw_ln in norm.split("\n"):
        if re.search(r"<!--\s*plan-hash\s*:", raw_ln, re.IGNORECASE):
            continue
        out_lines.append(raw_ln.rstrip(" \t"))
    joined = "\n".join(out_lines).strip()
    return joined.encode("utf-8")


def plan_hash(text: str) -> str:
    """Return canonical SHA-256 hash prefixed with 'sha256:'."""
    digest = hashlib.sha256(canonical_plan_bytes(text)).hexdigest()
    return f"sha256:{digest}"


def extract_items(text: str) -> list[dict[str, Any]]:
    """
    Extract [P1..Pn] items from plan markdown.
    Returns list of {"id": "P1", "description": str, "files": list[str], "ac": list[str]}.
    """
    norm = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    items: list[dict[str, Any]] = []
    seen_ids: dict[str, dict[str, Any]] = {}
    current_item: dict[str, Any] | None = None

    item_re = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?\[(P\d+)\](?:\*\*)?\s*(.+)$")
    for raw_ln in norm.split("\n"):
        s_ln = raw_ln.strip()
        m_item = item_re.match(s_ln)
        if m_item:
            pid = m_item.group(1).strip()
            desc = m_item.group(2).strip()
            files_owned: list[str] = []
            m_files = re.search(r"\(\s*files\s*:\s*([^)]+)\)", desc, re.IGNORECASE)
            if m_files:
                for tok in re.split(r"[,;]", m_files.group(1)):
                    clean_f = tok.strip().strip("`\"'")
                    if clean_f and clean_f not in files_owned:
                        files_owned.append(clean_f)
            ac_list: list[str] = []
            m_ac = re.search(r"\(\s*ac\s*:\s*([^)]+)\)", desc, re.IGNORECASE)
            if m_ac:
                for tok in re.split(r"[,;]", m_ac.group(1)):
                    clean_a = tok.strip()
                    if clean_a:
                        ac_list.append(clean_a)
            if pid in seen_ids:
                current_item = seen_ids[pid]
                for f in files_owned:
                    if f not in current_item["files"]:
                        current_item["files"].append(f)
                for a in ac_list:
                    if a not in current_item["ac"]:
                        current_item["ac"].append(a)
            else:
                current_item = {
                    "id": pid,
                    "description": desc,
                    "files": files_owned,
                    "ac": ac_list,
                }
                seen_ids[pid] = current_item
                items.append(current_item)
            continue

        if current_item is not None and s_ln:
            if s_ln.startswith("#"):
                current_item = None
                continue
            m_sub_files = re.match(r"^(?:[-*]\s*)?files\s*:\s*(.+)$", s_ln, re.IGNORECASE)
            if m_sub_files:
                for tok in re.split(r"[,;]", m_sub_files.group(1)):
                    clean_f = tok.strip().strip("`\"'")
                    if clean_f and clean_f not in current_item["files"]:
                        current_item["files"].append(clean_f)
            m_sub_ac = re.match(r"^(?:[-*]\s*)?ac\s*:\s*(.+)$", s_ln, re.IGNORECASE)
            if m_sub_ac:
                for tok in re.split(r"[,;]", m_sub_ac.group(1)):
                    clean_a = tok.strip()
                    if clean_a and clean_a not in current_item["ac"]:
                        current_item["ac"].append(clean_a)

    return items


def contract_lines(text: str) -> list[str]:
    """Extract bullet lines inside the '## Hợp đồng' / '## Contract' section."""
    norm = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    contracts: list[str] = []
    in_contract_sec = False
    for raw_ln in norm.split("\n"):
        s_ln = raw_ln.strip()
        if s_ln.startswith("## "):
            low_h = s_ln.lower()
            in_contract_sec = ("hợp đồng" in low_h) or ("hop dong" in low_h) or ("contract" in low_h)
            continue
        if in_contract_sec and (s_ln.startswith("-") or s_ln.startswith("*")):
            val = s_ln.lstrip("-* ").strip()
            if val:
                contracts.append(val)
    return contracts


def compute_risk(
    items: list[dict[str, Any]],
    contracts: list[str],
    diffstat: list[str] | str | None = None,
) -> dict[str, Any]:
    """
    Machine-computed risk assessment:
      - high if:
          * len(items) >= 4
          * contracts or items touch sensitive domains (auth, login, sso, token, payment, billing,
            migration, schema, public api, concurrency, race, idempoten)
          * 1 file belongs to >= 2 items
          * diffstat / item files touch >= 2 layers: app/services + db/migrations/infra/docker/k8s/ci
      - medium if len(items) >= 2 or len(contracts) >= 1
      - low otherwise
    """
    reasons: list[str] = []
    if len(items) >= 4:
        reasons.append("items>=4")

    combined_blob = " ".join(
        [str(c) for c in (contracts or [])]
        + [
            f"{it.get('description', '')} {' '.join(it.get('files') or [])}"
            for it in (items or [])
        ]
    ).lower()

    sensitive_patterns = [
        ("touches_auth", r"\b(?:auth|login|sso|token|jwt|oauth|session)\b"),
        ("touches_payment", r"\b(?:payment|billing|invoice|stripe|wallet)\b"),
        ("touches_migration", r"\b(?:migration|migrations|alter\s+table)\b"),
        ("touches_schema", r"\b(?:schema|unique\s*\(|foreign\s+key|create\s+table)\b"),
        ("touches_public_api", r"(?:\bpublic\s+api\b|\b(?:post|get|put|delete|patch)\s+/api/)"),
        ("touches_concurrency", r"\b(?:concurrency|race|idempoten|outbox|lock|mutex|transaction)\b"),
    ]
    for tag, pat in sensitive_patterns:
        if re.search(pat, combined_blob, re.IGNORECASE):
            reasons.append(tag)

    # Check shared files across >= 2 items
    file_owners: dict[str, list[str]] = {}
    all_files: list[str] = []
    for it in items or []:
        pid = str(it.get("id") or "")
        for f in it.get("files") or []:
            cf = str(f).strip()
            if not cf:
                continue
            all_files.append(cf)
            file_owners.setdefault(cf, [])
            if pid not in file_owners[cf]:
                file_owners[cf].append(pid)

    for cf, owners in sorted(file_owners.items()):
        if len(owners) >= 2:
            reasons.append(f"shared_file:{cf}")

    # Check diffstat / files touching >= 2 architectural layers
    diff_files: list[str] = list(all_files)
    if isinstance(diffstat, str):
        for ln in diffstat.splitlines():
            tok = ln.strip().split("|")[0].strip()
            if tok:
                diff_files.append(tok)
    elif isinstance(diffstat, list):
        diff_files.extend(str(x).strip() for x in diffstat if str(x).strip())

    touches_app = any(
        re.search(r"(?:^|/)(?:src|app|api|web|services)/", f, re.IGNORECASE)
        and not re.search(r"(?:^|/)(?:db/migrations|migrations|infra|docker|k8s|ci)/", f, re.IGNORECASE)
        for f in diff_files
    )
    touches_infra_or_db = any(
        re.search(
            r"(?:(?:^|/)(?:db/migrations|migrations|infra|k8s|ci|\.github)/|docker-compose|dockerfile|\.sql$)",
            f,
            re.IGNORECASE,
        )
        for f in diff_files
    )
    if touches_app and touches_infra_or_db:
        reasons.append("cross_layer:app+infra_db")

    if reasons:
        return {"level": "high", "reasons": reasons}
    if len(items or []) >= 2 or len(contracts or []) >= 1:
        med_reasons: list[str] = []
        if len(items or []) >= 2:
            med_reasons.append("items>=2")
        if len(contracts or []) >= 1:
            med_reasons.append("has_contracts")
        return {"level": "medium", "reasons": med_reasons}
    return {"level": "low", "reasons": ["single_item_low_surface"]}


def _is_valid_path_line(ref: str) -> bool:
    s = str(ref or "").strip()
    return bool(PATH_LINE_RE.match(s))


def extract_review_json_from_body(body: str) -> dict[str, Any] | None:
    """Extract a structured Critic review JSON object (`orca.critic.review/v1`) from markdown/text body."""
    txt = str(body or "")
    for m_f in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", txt, re.IGNORECASE):
        try:
            cand = json.loads(m_f.group(1))
            if isinstance(cand, dict) and (cand.get("schema") == CANON_VERSION or "coverage" in cand or "objections" in cand):
                return cand
        except Exception:
            pass
    m_raw = re.search(r"(\{[\s\S]*\"orca\.critic\.review/v1\"[\s\S]*\})", txt)
    if m_raw:
        try:
            cand = json.loads(m_raw.group(1))
            if isinstance(cand, dict):
                return cand
        except Exception:
            pass
    return None


def validate_review(
    review: dict[str, Any],
    plan_text: str,
    risk: dict[str, Any] | None = None,
    valid_reply_ids: set[str] | None = None,
) -> list[str]:
    """
    Validate Critic review JSON against canonical plan, computed risk, and resolution rules.
    Returns a list of human-readable guard violation messages (empty list == valid).
    """
    if not isinstance(review, dict):
        return ["[ORCA CRITIC SCHEMA GUARD] Review không phải JSON object hợp lệ."]

    violations: list[str] = []
    items = extract_items(plan_text)
    contracts = contract_lines(plan_text)
    if risk is None:
        risk = compute_risk(items, contracts)
    risk_level = str((risk or {}).get("level") or "high").lower()

    # 1. Schema
    if str(review.get("schema") or "") != CANON_VERSION:
        violations.append(
            f"[ORCA CRITIC SCHEMA GUARD] Trường 'schema' phải là '{CANON_VERSION}' (hiện tại: '{review.get('schema')}')."
        )

    # 2. Plan Hash Binding
    expected_hash = plan_hash(plan_text)
    rev_hash = str(review.get("planHash") or "").strip()
    if rev_hash != expected_hash:
        violations.append(
            f"[ORCA PLAN BINDING GUARD] Plan đã đổi sau khi duyệt: duyệt {rev_hash or '<trống>'}, hiện {expected_hash}. Chạy lại critic."
        )

    # 3. Verdict
    verdict = str(review.get("verdict") or "").strip()
    if verdict not in VALID_VERDICTS:
        violations.append(
            f"[ORCA CRITIC VERDICT GUARD] Trường 'verdict' không hợp lệ ('{verdict}'); phải thuộc {sorted(VALID_VERDICTS)}."
        )

    # 4. Coverage (100% [P1..Pn] with checked=true and >=1 path:line)
    expected_ids = [it["id"] for it in items]
    expected_set = set(expected_ids)
    cov_list = review.get("coverage")
    cov_map: dict[str, dict[str, Any]] = {}
    unknown_cov: list[str] = []
    if isinstance(cov_list, list):
        for entry in cov_list:
            if isinstance(entry, dict):
                cid = str(entry.get("item") or "").strip()
                if cid:
                    if expected_set and cid not in expected_set:
                        unknown_cov.append(cid)
                    cov_map[cid] = entry
    else:
        violations.append("[ORCA CRITIC COVERAGE GUARD] Trường 'coverage' phải là danh sách (array).")

    missing_or_bad_cov: list[str] = []
    for pid in expected_ids:
        c_entry = cov_map.get(pid)
        if not c_entry or c_entry.get("checked") is not True:
            missing_or_bad_cov.append(pid)
            continue
        ev_list = c_entry.get("evidence")
        if not isinstance(ev_list, list) or not ev_list or not any(_is_valid_path_line(x) for x in ev_list):
            missing_or_bad_cov.append(pid)

    if missing_or_bad_cov:
        violations.append(
            f"[ORCA CRITIC COVERAGE GUARD] Consensus thiếu phủ các hạng mục: {', '.join(missing_or_bad_cov)}. "
            "Mỗi [Pn] phải có coverage.checked=true + >=1 path:line."
        )
    if unknown_cov:
        violations.append(
            f"[ORCA CRITIC COVERAGE GUARD] Coverage chứa hạng mục lạ không thuộc Plan: {', '.join(unknown_cov)}."
        )

    # 5. Five System Engineering Axes
    axes = review.get("axes")
    if not isinstance(axes, dict):
        violations.append("[ORCA CRITIC AXES GUARD] Thiếu trường 'axes' (5 trục System Engineering).")
    else:
        missing_axes = [ax for ax in REQUIRED_AXES if ax not in axes or str(axes.get(ax)).lower() not in VALID_AXIS_VALUES]
        if missing_axes:
            violations.append(
                f"[ORCA CRITIC AXES GUARD] Thiếu hoặc sai giá trị trục thẩm định ({', '.join(missing_axes)}); "
                "mỗi trục phải có giá trị pass|warn|fail."
            )

    # 6. Objections & Challenge Quota
    objections = review.get("objections")
    if not isinstance(objections, list):
        objections = []
        violations.append("[ORCA CRITIC QUOTA GUARD] Trường 'objections' phải là danh sách (array).")

    seen_obj_ids: set[str] = set()
    blocker_count = 0
    for obj in objections:
        if not isinstance(obj, dict):
            violations.append("[ORCA CRITIC QUOTA GUARD] Phần tử trong 'objections' phải là object.")
            continue
        oid = str(obj.get("id") or "").strip()
        oitem = str(obj.get("item") or "").strip()
        osev = str(obj.get("severity") or "").strip().lower()
        if not oid:
            violations.append("[ORCA CRITIC QUOTA GUARD] Objection thiếu 'id'.")
        elif oid in seen_obj_ids:
            violations.append(f"[ORCA CRITIC QUOTA GUARD] Trùng lặp objection id '{oid}'.")
        else:
            seen_obj_ids.add(oid)
        if expected_set and oitem not in expected_set:
            violations.append(f"[ORCA CRITIC QUOTA GUARD] Objection '{oid}' tham chiếu item '{oitem}' không thuộc Plan.")
        if osev not in VALID_SEVERITIES:
            violations.append(f"[ORCA CRITIC QUOTA GUARD] Objection '{oid}' có severity '{osev}' không hợp lệ.")
        if osev == "blocker":
            blocker_count += 1

    if verdict == "CHANGES_REQUIRED":
        if len(objections) < 1:
            violations.append("[ORCA CRITIC QUOTA GUARD] Verdict CHANGES_REQUIRED phải có ít nhất 1 objection.")
        return violations

    if verdict == "CONSENSUS_REACHED":
        if risk_level == "high":
            if len(objections) < 3 or blocker_count < 1:
                violations.append(
                    f"[ORCA CRITIC QUOTA GUARD] Plan risk=high cần >=3 objection (>=1 blocker); hiện có {len(objections)}."
                )
        elif risk_level == "medium":
            if len(objections) < 1:
                violations.append(
                    f"[ORCA CRITIC QUOTA GUARD] Plan risk=medium cần >=1 objection; hiện có {len(objections)}."
                )
        else:
            rationale = review.get("no_objection_rationale")
            has_rationale = isinstance(rationale, list) and len([x for x in rationale if _is_valid_path_line(str(x))]) >= 3
            if len(objections) < 1 and not has_rationale:
                violations.append(
                    "[ORCA CRITIC QUOTA GUARD] Plan risk=low cần >=1 objection hoặc no_objection_rationale có >=3 path:line."
                )

    # 7. Resolutions lifecycle
    resolutions = review.get("resolutions")
    if not isinstance(resolutions, list):
        resolutions = []
    res_map: dict[str, dict[str, Any]] = {}
    for res in resolutions:
        if isinstance(res, dict):
            rid = str(res.get("objectionId") or "").strip()
            if rid:
                res_map[rid] = res

    unresolved_ids: list[str] = []
    for obj in objections:
        if not isinstance(obj, dict):
            continue
        oid = str(obj.get("id") or "").strip()
        osev = str(obj.get("severity") or "").strip().lower()
        res = res_map.get(oid)
        if not res:
            unresolved_ids.append(oid)
            if verdict == "CONSENSUS_REACHED":
                violations.append(
                    f"[ORCA CRITIC RESOLUTION GUARD] Objection {osev or 'blocker'} '{oid}' chưa đóng (status=unresolved)."
                )
            continue
        r_status = str(res.get("status") or "").strip().lower()
        if r_status not in VALID_RESOLUTION_STATUSES:
            violations.append(f"[ORCA CRITIC RESOLUTION GUARD] Resolution của '{oid}' có status='{r_status}' không hợp lệ.")
            continue
        if r_status == "unresolved":
            unresolved_ids.append(oid)
            if verdict == "CONSENSUS_REACHED":
                violations.append(
                    f"[ORCA CRITIC RESOLUTION GUARD] Objection {osev or 'blocker'} '{oid}' chưa đóng (status=unresolved)."
                )
        elif verdict == "CONSENSUS_REACHED":
            if osev == "blocker" and r_status == "accepted_risk" and risk_level == "high":
                violations.append(
                    f"[ORCA CRITIC RESOLUTION GUARD] Objection blocker '{oid}' không được phép 'accepted_risk' khi risk=high."
                )
            reply_id = str(res.get("leaderReplyId") or "").strip()
            if not reply_id:
                violations.append(
                    f"[ORCA CRITIC RESOLUTION GUARD] Resolution của '{oid}' thiếu 'leaderReplyId'."
                )
            elif valid_reply_ids is not None and reply_id not in valid_reply_ids:
                violations.append(
                    f"[ORCA CRITIC RESOLUTION GUARD] Resolution của '{oid}' có leaderReplyId='{reply_id}' không tồn tại trong messages."
                )

    if verdict == "DEADLOCK" and not unresolved_ids:
        violations.append("[ORCA CRITIC DEADLOCK GUARD] Verdict DEADLOCK phải có ít nhất 1 objection ở trạng thái unresolved.")

    return violations


def validate_independence(
    review: dict[str, Any],
    plan_text: str,
    hypothesis: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    hypothesis_md_text: str | None = None,
    first_challenge_at: str | None = None,
) -> list[str]:
    """
    C-FIX-23: Verify that Critic investigated independent sources outside the plan text
    and recorded a pre-plan hypothesis (for risk >= medium) whose SHA-256 matches
    `hypothesis.md`, whose `planHash` matches the plan, and whose `createdAt` is valid
    and precedes the first challenge message.
    """
    if not isinstance(review, dict):
        return ["[ORCA CRITIC INDEPENDENCE GUARD] Review không hợp lệ."]

    verdict = str(review.get("verdict") or "").strip()
    if verdict == "CHANGES_REQUIRED":
        return []

    items = extract_items(plan_text)
    contracts = contract_lines(plan_text)
    computed_risk = compute_risk(items, contracts)
    if risk is None:
        risk = computed_risk
    risk_level = str((risk or {}).get("level") or computed_risk.get("level") or "high").lower()
    expected_plan_hash = plan_hash(plan_text)

    violations: list[str] = []

    if risk_level in ("medium", "high"):
        if not isinstance(hypothesis, dict) or not hypothesis:
            violations.append(
                f"[ORCA CRITIC INDEPENDENCE GUARD] Thiếu hypothesis.json độc lập cho plan risk={risk_level}."
            )
        else:
            if str(hypothesis.get("schema") or "").strip() != HYPOTHESIS_SCHEMA:
                violations.append(
                    f"[ORCA CRITIC INDEPENDENCE GUARD] hypothesis.json sai schema ('{hypothesis.get('schema')}')."
                )
            hyp_plan_hash = str(hypothesis.get("planHash") or "").strip()
            if hyp_plan_hash != expected_plan_hash:
                violations.append(
                    f"[ORCA CRITIC INDEPENDENCE GUARD] hypothesis.json có planHash ('{hyp_plan_hash}') không khớp với planHash thực tế ('{expected_plan_hash}')."
                )
            hyp_refs = hypothesis.get("refs")
            valid_hyp_refs = [str(r).strip() for r in (hyp_refs or []) if _is_valid_path_line(str(r).strip())] if isinstance(hyp_refs, list) else []
            if not valid_hyp_refs:
                violations.append(
                    "[ORCA CRITIC INDEPENDENCE GUARD] hypothesis.json thiếu danh sách refs (path:line) hợp lệ."
                )
            created_at = str(hypothesis.get("createdAt") or "").strip()
            m_yr = re.match(r"^(\d{4})-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", created_at)
            if not m_yr or int(m_yr.group(1)) < 2024:
                violations.append(
                    f"[ORCA CRITIC INDEPENDENCE GUARD] hypothesis.json có createdAt không hợp lệ hoặc giả mạo ('{created_at}')."
                )
            elif first_challenge_at and str(first_challenge_at).strip():
                if created_at > str(first_challenge_at).strip():
                    violations.append(
                        f"[ORCA CRITIC INDEPENDENCE GUARD] hypothesis.json được tạo SAU challenge đầu tiên ({created_at} > {first_challenge_at})."
                    )
            h_hash = str(hypothesis.get("hash") or "").strip()
            rev_h_hash = str(review.get("hypothesisHash") or "").strip()
            if not h_hash or not re.fullmatch(r"sha256:[0-9a-f]{64}", h_hash):
                violations.append(
                    f"[ORCA CRITIC INDEPENDENCE GUARD] hypothesis.json thiếu trường hash sha256 hợp lệ ('{h_hash}')."
                )
            if not rev_h_hash or not re.fullmatch(r"sha256:[0-9a-f]{64}", rev_h_hash):
                violations.append(
                    f"[ORCA CRITIC INDEPENDENCE GUARD] Review thiếu trường hypothesisHash sha256 hợp lệ ('{rev_h_hash}')."
                )
            if h_hash and rev_h_hash and h_hash != rev_h_hash:
                violations.append(
                    f"[ORCA CRITIC INDEPENDENCE GUARD] hypothesisHash trong review ('{rev_h_hash}') không khớp hypothesis.json ('{h_hash}')."
                )
            if hypothesis_md_text is not None:
                actual_md_hash = "sha256:" + hashlib.sha256(hypothesis_md_text.encode("utf-8")).hexdigest()
                if h_hash != actual_md_hash or rev_h_hash != actual_md_hash:
                    violations.append(
                        f"[ORCA CRITIC INDEPENDENCE GUARD] Mã băm thực tế của hypothesis.md ('{actual_md_hash}') không khớp với hypothesis.json ('{h_hash}') hoặc review ('{rev_h_hash}')."
                    )

    indep_sources = review.get("independentSources")
    if not isinstance(indep_sources, list):
        indep_sources = []

    plan_norm = (plan_text or "").lower()
    plan_files = {str(f).strip().lower() for it in items for f in (it.get("files") or []) if str(f).strip()}

    truly_independent: list[str] = []
    for src in indep_sources:
        s_ref = str(src or "").strip()
        if not _is_valid_path_line(s_ref):
            continue
        file_part = s_ref.split(":", 1)[0].strip().lower()
        if s_ref.lower() not in plan_norm and file_part not in plan_files and file_part not in plan_norm:
            truly_independent.append(s_ref)

    if risk_level == "high" and len(truly_independent) < 2:
        violations.append(
            f"[ORCA CRITIC INDEPENDENCE GUARD] {len(truly_independent)} bằng chứng ngoài plan ở risk=high; cần >=2 path:line không có trong plan."
        )
    elif risk_level == "medium" and len(truly_independent) < 1:
        violations.append(
            f"[ORCA CRITIC INDEPENDENCE GUARD] {len(truly_independent)} bằng chứng ngoài plan ở risk=medium; cần >=1 path:line không có trong plan."
        )

    return violations


def validate_consensus_file(
    consensus: dict[str, Any],
    contracts: dict[str, Any],
    plan_text: str,
) -> list[str]:
    """
    Validate consensus.json against contracts.json and current plan text (including C-FIX-20 risk binding).
    """
    if not isinstance(consensus, dict):
        return ["[ORCA CONSENSUS PROVENANCE GUARD] consensus.json không hợp lệ hoặc bị hỏng."]

    violations: list[str] = []
    h_now = plan_hash(plan_text)
    c_hash = str(consensus.get("planHash") or "").strip()
    contracts_hash = str((contracts or {}).get("planHash") or "").strip()

    if c_hash != h_now:
        violations.append(
            f"[ORCA PLAN BINDING GUARD] Plan đã đổi sau khi duyệt: duyệt {c_hash or '<trống>'}, hiện {h_now}. Chạy lại critic."
        )
    elif contracts_hash and contracts_hash != h_now:
        violations.append(
            f"[ORCA PLAN BINDING GUARD] Plan đã đổi sau khi duyệt: duyệt {contracts_hash}, hiện {h_now}. Chạy lại critic."
        )

    # C-FIX-20: Always recompute risk from plan_text and reject any downgrade/mismatch in contracts.json or consensus.json
    computed_risk = compute_risk(extract_items(plan_text), contract_lines(plan_text))
    actual_level = str(computed_risk.get("level") or "high").lower()
    if isinstance(contracts, dict) and isinstance(contracts.get("riskLevel"), dict):
        c_level = str(contracts["riskLevel"].get("level") or "").strip().lower()
        if c_level and c_level != actual_level:
            violations.append(
                f"[ORCA RISK BINDING GUARD] riskLevel trong contracts.json ('{c_level}') đã bị sửa lệch với risk tính từ plan ('{actual_level}')!"
            )
    if isinstance(consensus.get("risk"), dict):
        cons_level = str(consensus["risk"].get("level") or "").strip().lower()
        if cons_level and cons_level != actual_level:
            violations.append(
                f"[ORCA RISK BINDING GUARD] risk trong consensus.json ('{cons_level}') lệch với risk tính từ plan ('{actual_level}')!"
            )

    verdict = str(consensus.get("verdict") or "").strip()
    if verdict == "DEADLOCK":
        violations.append(
            "[ORCA CRITIC DEADLOCK GUARD] Còn bất đồng chưa giải quyết. Mở human gate: "
            f"orca-team gate create --from-consensus {consensus.get('runId') or '<run>'}"
        )
    elif verdict != "CONSENSUS_REACHED":
        violations.append(
            f"[ORCA PLAN BINDING GUARD] Consensus chưa đạt (verdict='{verdict}')."
        )

    if str(consensus.get("schema") or "") != CONSENSUS_SCHEMA:
        violations.append(
            f"[ORCA CONSENSUS PROVENANCE GUARD] consensus.json sai schema ('{consensus.get('schema')}')."
        )

    msg_ids = consensus.get("messageIds")
    if not isinstance(msg_ids, dict):
        violations.append(
            "[ORCA CONSENSUS PROVENANCE GUARD] consensus.json không khớp message log (thiếu trường messageIds)."
        )
    else:
        challenges = msg_ids.get("challenges")
        replies = msg_ids.get("replies")
        final_id = str(msg_ids.get("final") or "").strip()
        if (
            not isinstance(challenges, list)
            or not challenges
            or not isinstance(replies, list)
            or not replies
            or not final_id
        ):
            violations.append(
                "[ORCA CONSENSUS PROVENANCE GUARD] consensus.json không khớp message log (thiếu challenges/replies/final messageId)."
            )

    return violations


# ======================================================================
# C-FIX-13 .. C-FIX-19: Canonical Runner Allowlist, Test-Tree Integrity,
# Ledger HMAC & Canonical Gate Command Resolution
# ======================================================================

FORBIDDEN_SHELL_OPS = {"||", "&&", ";", "|", ">", ">>", "<", "&", "2>&1", "1>&2"}
FORBIDDEN_BINARIES = {
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish",
    "env", "eval", "exec", "source", "true", "false", ":", "echo",
    "printf", "cat", "ls", "pwd", "sleep", "whoami", "date", "export",
    "head", "tail", "wc", "touch", "rm", "cp", "mv", "chmod", "chown",
    "id", "uname", "hostname",
}


def normalize_cmd_tokens(cmd_tokens: list[str]) -> list[str]:
    """Normalize command tokens (e.g. map any python/python3 interpreter path to 'python3') for stable hashing."""
    if not cmd_tokens:
        return []
    out = [str(x).strip() for x in cmd_tokens if str(x).strip()]
    if not out:
        return []
    exe_b = os.path.basename(out[0]).lower()
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", exe_b):
        out[0] = "python3"
    elif exe_b in ("pytest", "py.test"):
        out[0] = "pytest"
    return out


def canonical_cmd_hash(cmd_tokens: list[str]) -> str:
    """Compute 16-hex SHA-256 hash over normalized command tokens."""
    norm = normalize_cmd_tokens(cmd_tokens)
    s = " ".join(norm)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def validate_runner_allowlist(
    cmd_tokens: list[str],
    kind: str = "test",
    worktree_dir: Path | str | None = None,
) -> tuple[bool, str]:
    """
    C-FIX-13, C-FIX-19 & C-FIX-28: Strict allowlist + full-argv path confinement for `orca-team verify` and Guard.
    Rejects shell wrappers (bash/sh/zsh), inline flags (-c, -e), relative scripts (./x.sh),
    arbitrary scripts (`python3 fake.py`), absolute paths outside `worktree_dir`, and `..` path traversal.
    """
    if not isinstance(cmd_tokens, list) or not cmd_tokens:
        return False, "[ORCA VERIFY RUNNER ALLOWLIST] Lệnh kiểm thử rỗng!"

    tokens = [str(x).strip() for x in cmd_tokens if str(x).strip()]
    if not tokens:
        return False, "[ORCA VERIFY RUNNER ALLOWLIST] Lệnh kiểm thử rỗng!"

    cmd_str = " ".join(tokens)
    for tok in tokens:
        if tok in FORBIDDEN_SHELL_OPS or any(op in tok for op in ("||", "&&", ";", "|", "$(", "`")):
            return (
                False,
                f"[ORCA VERIFY GUARD] CẤM dùng toán tử nối chuỗi/chuyển hướng shell ('{tok}') trong 'orca-team verify'!",
            )

    t0 = tokens[0]
    exe = os.path.basename(t0).lower()

    if (t0.startswith("./") or t0.startswith("../")) and exe != "gradlew":
        return (
            False,
            f"[ORCA VERIFY RUNNER ALLOWLIST] CẤM thực thi script đường dẫn tương đối ('{t0}')! Chỉ chấp nhận trình chạy chuẩn trong allowlist.",
        )

    if exe in FORBIDDEN_BINARIES:
        return (
            False,
            f"[ORCA VERIFY RUNNER ALLOWLIST] CẤM dùng shell wrapper hoặc lệnh hệ thống ('{t0}') làm trình chạy kiểm thử! "
            "Allowlist hợp lệ: pytest, python3 -m unittest|pytest, npm/pnpm/yarn/bun test, npx jest|vitest|playwright, go test, cargo test, dotnet test, mvn/gradle test, ctest, make test.",
        )

    wt_real = os.path.realpath(str(worktree_dir)) if worktree_dir else ""
    dir_flags = {
        "-s", "--start-directory",
        "-t", "--top-level-directory",
        "-C", "--directory",
        "--prefix",
        "--rootdir", "--confcutdir",
        "-f", "--file", "--makefile",
        "--config", "--manifest-path",
        "--project", "--cwd",
        "--test-dir",
    }

    idx = 1
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in ("-c", "-e", "--eval", "--command") or (tok.startswith("-c") and exe != "pytest" and not tok.startswith("--")):
            return (
                False,
                f"[ORCA VERIFY RUNNER ALLOWLIST] CẤM dùng cờ thực thi mã inline ('{tok}') trong '{cmd_str}'! "
                "Mọi bài test phải nằm trong cây thư mục test chuẩn của dự án và chạy qua test runner allowlist.",
            )

        # C-FIX-28: Check flag=value or flag <value> path arguments and standalone path tokens
        cand_paths: list[str] = []
        if "=" in tok and tok.startswith("-"):
            f_k, f_v = tok.split("=", 1)
            if f_k in dir_flags or f_v.startswith("/") or ".." in f_v.split("/"):
                cand_paths.append(f_v)
        elif tok in dir_flags:
            if idx + 1 >= len(tokens):
                return False, f"[ORCA VERIFY RUNNER ALLOWLIST] Cờ '{tok}' thiếu giá trị đường dẫn hợp lệ trong '{cmd_str}'!"
            cand_paths.append(tokens[idx + 1])
            idx += 1
        elif not tok.startswith("-"):
            if tok.startswith("/") or tok.startswith("~") or ".." in tok.replace("\\", "/").split("/"):
                cand_paths.append(tok)

        for cp in cand_paths:
            cp_clean = cp.strip().strip("'\"")
            if not cp_clean:
                continue
            norm_parts = [p for p in cp_clean.replace("\\", "/").split("/") if p]
            if ".." in norm_parts or cp_clean.startswith("~"):
                return (
                    False,
                    f"[ORCA VERIFY RUNNER ALLOWLIST] CẤM dùng đường dẫn vượt biên ('{cp_clean}') trong lệnh '{cmd_str}'! "
                    "Mọi đường dẫn kiểm thử bắt buộc nằm bên trong worktree dự án.",
                )
            if os.path.isabs(cp_clean):
                if not wt_real:
                    return (
                        False,
                        f"[ORCA VERIFY RUNNER ALLOWLIST] CẤM truyền đường dẫn tuyệt đối ('{cp_clean}') trong lệnh '{cmd_str}'! "
                        "Chỉ được phép truyền đường dẫn tương đối bên trong worktree dự án.",
                    )
                rp = os.path.realpath(cp_clean)
                if rp != wt_real and not rp.startswith(wt_real + os.sep):
                    return (
                        False,
                        f"[ORCA VERIFY RUNNER ALLOWLIST] Đường dẫn tuyệt đối '{cp_clean}' (realpath='{rp}') nằm NGOÀI worktree '{wt_real}'! "
                        "CẤM trỏ trình chạy kiểm thử ra thư mục ngoài repo.",
                    )
            elif wt_real:
                rp = os.path.realpath(os.path.join(wt_real, cp_clean))
                if rp != wt_real and not rp.startswith(wt_real + os.sep):
                    return (
                        False,
                        f"[ORCA VERIFY RUNNER ALLOWLIST] Đường dẫn '{cp_clean}' (realpath='{rp}') trỏ ra NGOÀI worktree '{wt_real}'!",
                    )
        idx += 1

    eff_kind = (kind or "test").strip().lower()
    if eff_kind == "test":
        # 1. pytest
        if exe in ("pytest", "py.test"):
            return True, ""
        # 2. python -m unittest|pytest
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?", exe):
            if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in ("unittest", "pytest"):
                return True, ""
            return (
                False,
                f"[ORCA VERIFY RUNNER ALLOWLIST] Lệnh Python '{cmd_str}' bị từ chối! "
                "Khi chạy test với Python, bắt buộc dùng 'python3 -m unittest ...' hoặc 'python3 -m pytest ...' (CẤM chạy trực tiếp file .py tự tạo).",
            )
        # 3. npm / pnpm / yarn / bun test
        if exe in ("npm", "pnpm", "yarn", "bun"):
            if len(tokens) >= 2 and (
                tokens[1] in ("test", "t", "tst")
                or (len(tokens) >= 3 and tokens[1] == "run" and tokens[2].startswith("test"))
            ):
                return True, ""
        # 4. npx jest|vitest|playwright|mocha|cypress
        if exe == "npx":
            if len(tokens) >= 2 and tokens[1] in ("jest", "vitest", "playwright", "mocha", "cypress"):
                return True, ""
        # 5. Direct test runners
        if exe in ("jest", "vitest", "playwright", "mocha", "cypress", "ctest"):
            return True, ""
        # 6. go / cargo / dotnet test
        if exe in ("go", "cargo", "dotnet"):
            if len(tokens) >= 2 and tokens[1] == "test":
                return True, ""
        # 7. mvn / gradle / gradlew test|check
        if exe in ("mvn", "gradle", "gradlew"):
            if len(tokens) >= 2 and any(x in ("test", "check") for x in tokens[1:]):
                return True, ""
        # 8. make test|check
        if exe == "make":
            if len(tokens) >= 2 and tokens[1] in ("test", "check"):
                return True, ""
        return (
            False,
            f"[ORCA VERIFY RUNNER ALLOWLIST] Lệnh '{cmd_str}' không thuộc allowlist trình chạy test chuẩn! "
            "Allowlist hợp lệ: pytest, python3 -m unittest|pytest, npm/pnpm/yarn/bun test, npx jest|vitest|playwright, go test, cargo test, dotnet test, mvn/gradle test, ctest, make test.",
        )

    # kind in ("build", "lint", "typecheck")
    if eff_kind in ("build", "lint", "typecheck"):
        if exe in ("npm", "pnpm", "yarn", "bun"):
            if len(tokens) >= 2 and (
                tokens[1] in ("build", "lint", "typecheck", "check", "compile")
                or (
                    len(tokens) >= 3
                    and tokens[1] == "run"
                    and any(tokens[2].startswith(p) for p in ("build", "lint", "typecheck", "check", "compile"))
                )
            ):
                return True, ""
        if exe == "npx":
            if len(tokens) >= 2 and tokens[1] in ("tsc", "eslint", "prettier", "biome", "ruff", "flake8", "mypy", "pyright"):
                return True, ""
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?", exe):
            if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in ("py_compile", "compileall", "ruff", "flake8", "mypy", "black"):
                return True, ""
        if exe in ("ruff", "flake8", "mypy", "pyright", "eslint", "tsc", "prettier", "biome"):
            return True, ""
        if exe in ("go", "cargo", "dotnet", "make", "mvn", "gradle", "gradlew"):
            if len(tokens) >= 2 and tokens[1] in ("build", "vet", "check", "clippy", "lint", "typecheck", "compile", "assemble"):
                return True, ""
        return (
            False,
            f"[ORCA VERIFY RUNNER ALLOWLIST] Lệnh {eff_kind} '{cmd_str}' không thuộc allowlist chuẩn "
            "(npm/pnpm/yarn/bun run build|lint|typecheck, python3 -m py_compile|ruff|flake8|mypy, npx tsc|eslint, cargo/go build|check)!",
        )

    return False, f"[ORCA VERIFY RUNNER ALLOWLIST] Loại kiểm thử kind='{eff_kind}' không hợp lệ!"


def is_test_file_path(rel_path: str) -> bool:
    """Return True if a relative POSIX file path is a test file or test runner configuration (C-FIX-29)."""
    p = str(rel_path or "").strip().replace("\\", "/").lstrip("./")
    if not p:
        return False
    parts = [x.lower() for x in p.split("/") if x]
    if any(x in (".git", ".orca", ".orca-cache", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv", "dist", "build") for x in parts):
        return False
    base = parts[-1]
    if base.endswith((".pyc", ".pyo", ".pyd")) or base == ".ds_store":
        return False
    if any(d in ("tests", "test", "__tests__", "spec", "e2e") for d in parts[:-1]):
        return True
    if (
        re.match(r"^test_.*\.(?:py|ts|tsx|js|jsx|go|rs)$", base, re.IGNORECASE)
        or re.match(r"^.*_test\.(?:py|go|rs)$", base, re.IGNORECASE)
        or re.match(r"^.*\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs|cjs|py)$", base, re.IGNORECASE)
    ):
        return True
    if base in (
        "pytest.ini",
        "pyproject.toml",
        "tox.ini",
        "tsconfig.spec.json",
        "package.json",
        "makefile",
        "gnumakefile",
        "go.mod",
        "cargo.toml",
        "conftest.py",
        "setup.cfg",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    ) or re.match(r"^(?:jest|vitest|playwright|cypress|mocha)\.config\..+$", base, re.IGNORECASE):
        return True
    return False


def is_valid_item_test_pattern(pat: str, item_id: str = "") -> tuple[bool, str]:
    """
    C-FIX-30: Reject weak/catch-all test patterns (e.g. '.', '.*', '^', '$', '\\w+', 'test')
    that match arbitrary output without binding to the plan item ID (e.g. P1 -> p1) or a concrete test identifier.
    """
    s = str(pat or "").strip()
    if not s:
        return False, "[ORCA PLAN-COVERAGE GUARD] test_pattern rỗng!"
    stripped = re.sub(r"^\(\?[a-zA-Z]+\)", "", s).strip()
    alnum = re.findall(r"[A-Za-z0-9_]+", stripped)
    joined_alnum = "".join(alnum).lower()
    if (
        stripped in (".", ".*", ".+", "^", "$", "^.*$", "^.+$", r"\w+", r"\S+", r"\d+")
        or len(joined_alnum) < 4
        or joined_alnum in ("test", "tests", "pass", "passed", "ok", "ran", "suite")
    ):
        return (
            False,
            f"[ORCA PLAN-COVERAGE GUARD] test_pattern '{s}' quá yếu / khớp rỗng (catch-all)! "
            f"CẤM đổi test_pattern thành '{s}' để qua mặt kiểm tra độ phủ [{item_id or 'Pn'}].",
        )
    if item_id:
        pk_low = item_id.strip().lower()
        if pk_low not in s.lower():
            return (
                False,
                f"[ORCA PLAN-COVERAGE GUARD] test_pattern '{s}' của hạng mục [{item_id}] không chứa định danh '{pk_low}'! "
                f"Mỗi hạng mục [{item_id}] bắt buộc ánh xạ tới tên test chứa '{pk_low}' (ví dụ: test_{pk_low}).",
            )
    return True, ""


def compute_policy_anchor_hmac(anchor: dict[str, Any], secret: bytes) -> str:
    """C-FIX-27: Compute HMAC-SHA256 for a pinned policy anchor."""
    clean = {k: v for k, v in anchor.items() if k != "anchor_hmac"}
    canon = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret, canon, hashlib.sha256).hexdigest()


def verify_policy_anchor_hmac(anchor: dict[str, Any], secret: bytes) -> bool:
    """C-FIX-27: Verify HMAC-SHA256 on a pinned policy anchor."""
    sig = str(anchor.get("anchor_hmac") or "").strip()
    if not sig or not re.fullmatch(r"[0-9a-f]{64}", sig):
        return False
    expected = compute_policy_anchor_hmac(anchor, secret)
    return hmac.compare_digest(sig, expected)


def write_pinned_policy_anchor(
    ev_base_dir: Path | str,
    run_id: str,
    plan_hash_val: str,
    canonical_cmds: list[tuple[str, list[str]]],
    item_test_patterns: dict[str, str],
    test_allowlist: list[str] | None = None,
) -> tuple[Path | None, str]:
    """
    C-FIX-27: Write 0600 HMAC-signed policy anchor outside the worktree at `<ev_base_dir>/<run_id>/.policy_anchor.json`
    so agent edits to `contracts.json` or `orca-team.json` cannot alter the canonical test command or test patterns.
    """
    secret, err = get_or_create_hmac_secret(ev_base_dir, create_if_missing=True)
    if err or not secret:
        return None, err or "Cannot create HMAC secret"
    run_ev_dir = Path(ev_base_dir) / (run_id or "default")
    run_ev_dir.mkdir(parents=True, exist_ok=True)
    anchor_path = run_ev_dir / ".policy_anchor.json"
    cmds_norm = [
        {
            "kind": k,
            "cmd": normalize_cmd_tokens(toks),
            "cmd_hash": canonical_cmd_hash(toks),
        }
        for k, toks in canonical_cmds
    ]
    anchor_obj: dict[str, Any] = {
        "schema": "orca.policy.anchor/v1",
        "runId": run_id,
        "planHash": plan_hash_val,
        "canonicalCommands": cmds_norm,
        "itemTestPatterns": dict(sorted((item_test_patterns or {}).items())),
        "testAllowlist": sorted(set(test_allowlist or [])),
    }
    anchor_obj["anchor_hmac"] = compute_policy_anchor_hmac(anchor_obj, secret)
    fd = os.open(str(anchor_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as af:
        af.write(json.dumps(anchor_obj, ensure_ascii=False, indent=2) + "\n")
    os.chmod(str(anchor_path), 0o600)
    return anchor_path, ""


def load_pinned_policy_anchor(ev_base_dir: Path | str, run_id: str) -> tuple[dict[str, Any] | None, str]:
    """
    C-FIX-27: Load and verify `<ev_base_dir>/<run_id>/.policy_anchor.json` if present.
    Returns (anchor_dict, error_str). If file does not exist, returns (None, "").
    """
    anchor_path = Path(ev_base_dir) / (run_id or "default") / ".policy_anchor.json"
    if not anchor_path.exists():
        return None, ""
    try:
        st = os.lstat(str(anchor_path))
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o777) != 0o600:
            return None, f"[ORCA POLICY-ANCHOR GUARD] File neo chính sách '{anchor_path}' có quyền hoặc chủ sở hữu không hợp lệ!"
        secret, err = get_or_create_hmac_secret(ev_base_dir, create_if_missing=False)
        if err or not secret:
            return None, err or "[ORCA POLICY-ANCHOR GUARD] Thiếu khóa HMAC để xác thực policy anchor!"
        obj = json.loads(anchor_path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict) or not verify_policy_anchor_hmac(obj, secret):
            return None, f"[ORCA POLICY-ANCHOR GUARD] Chữ ký HMAC của '{anchor_path}' không hợp lệ hoặc đã bị giả mạo!"
        return obj, ""
    except Exception as exc:
        return None, f"[ORCA POLICY-ANCHOR GUARD] Không thể đọc '{anchor_path}' ({exc})!"


def compute_test_tree_hash(repo_dir: Path | str) -> tuple[str, list[str]]:
    """
    C-FIX-14: Compute SHA-256 hash of all test files and test configs in `repo_dir`.
    Returns (hash_str, sorted_rel_paths). If no test files exist, returns ("", []).
    """
    root = Path(repo_dir).resolve()
    if not root.is_dir():
        return "", []
    ignore_dirs = {".git", ".orca", ".orca-cache", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv", "dist", "build"}
    matched_files: list[str] = []
    for cur_root, dirs, files in os.walk(root):
        dirs[:] = sorted([d for d in dirs if d not in ignore_dirs and not d.startswith(".orca")])
        for fn in sorted(files):
            fp = Path(cur_root) / fn
            try:
                rel_p = fp.relative_to(root).as_posix()
            except Exception:
                continue
            if is_test_file_path(rel_p):
                matched_files.append(rel_p)
    matched_files.sort()
    if not matched_files:
        return "", []
    h = hashlib.sha256()
    for rel_p in matched_files:
        fp = root / rel_p
        try:
            raw_b = fp.read_bytes()
        except Exception:
            raw_b = b""
        h.update(rel_p.encode("utf-8") + b"\0" + hashlib.sha256(raw_b).digest() + b"\n")
    return f"sha256:{h.hexdigest()}", matched_files


def get_or_create_hmac_secret(ev_base_dir: Path | str, create_if_missing: bool = True) -> tuple[bytes | None, str]:
    """
    C-FIX-18: Load or create 0600 HMAC secret in `ev_base_dir/.hmac_secret`.
    Verifies file is not a symlink, belongs to current UID, and has mode 0600.
    Returns (secret_bytes, error_reason).
    """
    base = Path(ev_base_dir)
    sec_path = base / ".hmac_secret"
    if not sec_path.exists():
        if not create_if_missing:
            return None, f"[ORCA LEDGER-INTEGRITY GUARD] Không tìm thấy khóa HMAC ('{sec_path}')! Ledger chưa được ký bởi orca-team verify."
        try:
            base.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(sec_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as sf:
                sf.write(secrets.token_hex(32))
            os.chmod(str(sec_path), 0o600)
        except Exception as exc:
            return None, f"[ORCA LEDGER-INTEGRITY GUARD] Không thể khởi tạo khóa HMAC ('{exc}')!"

    try:
        st = os.lstat(str(sec_path))
        if stat.S_ISLNK(st.st_mode):
            return None, f"[ORCA LEDGER-INTEGRITY GUARD] Khóa HMAC '{sec_path}' là symlink bất hợp lệ!"
        if st.st_uid != os.getuid():
            return None, f"[ORCA LEDGER-INTEGRITY GUARD] Khóa HMAC '{sec_path}' không thuộc UID hiện tại ({st.st_uid} != {os.getuid()})!"
        if (st.st_mode & 0o777) != 0o600:
            return None, f"[ORCA LEDGER-INTEGRITY GUARD] Quyền file khóa HMAC '{sec_path}' không đúng 0600 ({oct(st.st_mode & 0o777)})!"
        raw = sec_path.read_text(encoding="utf-8").strip()
        if len(raw) < 32:
            return None, f"[ORCA LEDGER-INTEGRITY GUARD] Khóa HMAC '{sec_path}' bị hỏng hoặc quá ngắn!"
        return raw.encode("utf-8"), ""
    except Exception as exc:
        return None, f"[ORCA LEDGER-INTEGRITY GUARD] Không thể đọc khóa HMAC '{sec_path}' ({exc})!"


def compute_row_hmac(row: dict[str, Any], secret: bytes) -> str:
    """Compute HMAC-SHA256 for a ledger row (excluding `row_hmac` and internal `_file` keys)."""
    clean = {k: v for k, v in row.items() if k not in ("row_hmac", "_file")}
    canon = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret, canon, hashlib.sha256).hexdigest()


def verify_row_hmac(row: dict[str, Any], secret: bytes) -> bool:
    """Constant-time verification of `row_hmac` on a ledger row."""
    sig = str(row.get("row_hmac") or "").strip()
    if not sig or not re.fullmatch(r"[0-9a-f]{64}", sig):
        return False
    expected = compute_row_hmac(row, secret)
    return hmac.compare_digest(sig, expected)


def resolve_canonical_gate_commands(
    worktree_dir: Path | str,
    main_root: Path | str | None = None,
    contracts_obj: dict[str, Any] | None = None,
    cfg_obj: dict[str, Any] | None = None,
) -> list[tuple[str, list[str]]]:
    """
    C-FIX-13 & C-FIX-17: Resolve canonical (kind, cmd_tokens) pairs from:
    1) contracts.json (`verify_cmd` or `items[].verify.cmd`),
    2) orca-team.json (`gateCommands` with list-of-tokens `cmd`),
    3) repository manifests in `worktree_dir` (fallback `main_root`).
    Never reads from ledger and never falls back to `git status --porcelain`.
    """
    wt = Path(worktree_dir).resolve() if worktree_dir else Path.cwd().resolve()
    mr = Path(main_root).resolve() if main_root else wt
    resolved: list[tuple[str, list[str]]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _add(k: str, toks: list[str]) -> None:
        clean_t = [str(x).strip() for x in toks if str(x).strip()]
        if not clean_t:
            return
        ok, _ = validate_runner_allowlist(clean_t, k, worktree_dir=wt)
        if not ok:
            return
        norm_key = (k, " ".join(normalize_cmd_tokens(clean_t)))
        if norm_key not in seen_keys:
            seen_keys.add(norm_key)
            resolved.append((k, clean_t))

    # 1. From contracts.json
    if isinstance(contracts_obj, dict):
        top_v = contracts_obj.get("verify_cmd")
        if isinstance(top_v, list) and top_v:
            _add("test", [str(x) for x in top_v])
        elif isinstance(top_v, str) and top_v.strip():
            _add("test", shlex.split(top_v.strip()))
        for it in contracts_obj.get("items") or []:
            if isinstance(it, dict):
                v_obj = it.get("verify")
                if isinstance(v_obj, dict) and v_obj.get("cmd"):
                    vc = v_obj["cmd"]
                    toks = [str(x) for x in vc] if isinstance(vc, list) else shlex.split(str(vc))
                    _add("test", toks)
                elif it.get("verify_cmd"):
                    vc = it["verify_cmd"]
                    toks = [str(x) for x in vc] if isinstance(vc, list) else shlex.split(str(vc))
                    _add("test", toks)

    # 2. From orca-team.json `gateCommands`
    if isinstance(cfg_obj, dict):
        g_cmds = cfg_obj.get("gateCommands")
        if isinstance(g_cmds, list):
            for entry in g_cmds:
                if isinstance(entry, dict) and entry.get("cmd"):
                    k = str(entry.get("kind") or "test").strip().lower()
                    raw_c = entry["cmd"]
                    toks = [str(x) for x in raw_c] if isinstance(raw_c, list) else shlex.split(str(raw_c))
                    _add(k, toks)

    # 3. From repository manifests (prefer worktree_dir if it has a manifest/tests, else main_root)
    has_test_cmd = any(k == "test" for k, _ in resolved)
    if not has_test_cmd:
        for cand_dir in (wt, mr):
            if not cand_dir.is_dir():
                continue
            pkg_p = cand_dir / "package.json"
            if pkg_p.is_file():
                try:
                    pkg = json.loads(pkg_p.read_text(encoding="utf-8", errors="ignore"))
                    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
                    if "lint" in scripts:
                        _add("lint", ["npm", "run", "lint"])
                    if "typecheck" in scripts:
                        _add("typecheck", ["npm", "run", "typecheck"])
                    if "test" in scripts:
                        _add("test", ["npm", "test"])
                        break
                except Exception:
                    pass
            if (cand_dir / "pytest.ini").is_file():
                _add("test", [sys.executable, "-m", "pytest"])
                break
            if (cand_dir / "pyproject.toml").is_file() or (cand_dir / "tests").is_dir() or (cand_dir / "test").is_dir():
                t_dir = "tests" if (cand_dir / "tests").is_dir() or not (cand_dir / "test").is_dir() else "test"
                _add("test", [sys.executable, "-m", "unittest", "discover", "-s", t_dir])
                break
            if (cand_dir / "go.mod").is_file():
                _add("test", ["go", "test", "./..."])
                break
            if (cand_dir / "Cargo.toml").is_file():
                _add("test", ["cargo", "test"])
                break

    return resolved


def _cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="orca_critic_gate.py")
    sub = parser.add_subparsers(dest="cmd")

    p_val = sub.add_parser("validate", help="Validate a critic review JSON against a plan file")
    p_val.add_argument("--plan", required=True, help="Path to plan markdown file")
    p_val.add_argument("--review", required=True, help="Path to review JSON file")
    p_val.add_argument("--hypothesis", default="", help="Optional path to hypothesis JSON file")
    p_val.add_argument("--risk", default="auto", help="auto|low|medium|high")

    p_hash = sub.add_parser("hash", help="Compute canonical plan hash and risk")
    p_hash.add_argument("--plan", required=True, help="Path to plan markdown file")

    args = parser.parse_args(argv)
    if args.cmd == "hash":
        plan_txt = Path(args.plan).read_text(encoding="utf-8")
        items = extract_items(plan_txt)
        contracts = contract_lines(plan_txt)
        risk = compute_risk(items, contracts)
        print(
            json.dumps(
                {
                    "planHash": plan_hash(plan_txt),
                    "risk": risk,
                    "items": items,
                    "contracts": contracts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.cmd == "validate":
        plan_txt = Path(args.plan).read_text(encoding="utf-8")
        review_obj = json.loads(Path(args.review).read_text(encoding="utf-8"))
        hyp_obj = None
        if args.hypothesis and Path(args.hypothesis).is_file():
            hyp_obj = json.loads(Path(args.hypothesis).read_text(encoding="utf-8"))
        items = extract_items(plan_txt)
        contracts = contract_lines(plan_txt)
        risk = (
            compute_risk(items, contracts)
            if args.risk == "auto"
            else {"level": args.risk, "reasons": [f"manual:{args.risk}"]}
        )
        viols = validate_review(review_obj, plan_txt, risk)
        viols.extend(validate_independence(review_obj, plan_txt, hyp_obj, risk))
        print(json.dumps({"planHash": plan_hash(plan_txt), "risk": risk, "violations": viols}, ensure_ascii=False, indent=2))
        return 0 if not viols else 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv[1:]))
