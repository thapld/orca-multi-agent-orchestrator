#!/usr/bin/env python3
"""
Shared pure-Python module (stdlib only, no DB access) for Orca Critic Consensus Gate.
Provides canonical plan hashing, item/contract extraction, automated risk scoring,
structured review validation, independence verification, and consensus file validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
) -> list[str]:
    """
    Verify that Critic investigated independent sources outside the plan text
    and recorded a pre-plan hypothesis (for risk >= medium).
    """
    if not isinstance(review, dict):
        return ["[ORCA CRITIC INDEPENDENCE GUARD] Review không hợp lệ."]

    verdict = str(review.get("verdict") or "").strip()
    if verdict == "CHANGES_REQUIRED":
        return []

    items = extract_items(plan_text)
    contracts = contract_lines(plan_text)
    if risk is None:
        risk = compute_risk(items, contracts)
    risk_level = str((risk or {}).get("level") or "high").lower()

    violations: list[str] = []

    if risk_level in ("medium", "high"):
        if not isinstance(hypothesis, dict) or not hypothesis:
            violations.append(
                f"[ORCA CRITIC INDEPENDENCE GUARD] Thiếu hypothesis.json độc lập cho plan risk={risk_level}."
            )
        else:
            h_hash = str(hypothesis.get("hash") or "").strip()
            rev_h_hash = str(review.get("hypothesisHash") or "").strip()
            if h_hash and rev_h_hash and h_hash != rev_h_hash:
                violations.append(
                    f"[ORCA CRITIC INDEPENDENCE GUARD] hypothesisHash trong review ('{rev_h_hash}') không khớp hypothesis.json ('{h_hash}')."
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
    Validate consensus.json against contracts.json and current plan text.
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
