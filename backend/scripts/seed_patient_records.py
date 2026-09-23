"""ClinEvidence 种子装机 CLI:清单预检(validate)与经同一管线的患者导入(seed)。

材料契约见执行计划 6.6 节:
- dataset_manifest.json:数据集版本、患者归属、文件 SHA-256;
- assignment_review.json:归属审核确认(manifest_hash 绑定);
- evaluation_scope_map.jsonl:题库 session 到患者与文档范围的审核映射。

validate 只读,不写任何业务事实;seed 经 patient_record_ingest_service 创建批次,
等待 worker 解析与发布,不直接写表、向量或快照。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
for import_path in (APP_ROOT, APP_ROOT / "package"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_materials(data_dir: Path) -> tuple[dict, dict, list[dict]]:
    """读取并校验三份装机材料;manifest_hash 与审核绑定必须一致。"""
    manifest = json.loads((data_dir / "dataset_manifest.json").read_text(encoding="utf-8"))
    review = json.loads((data_dir / "assignment_review.json").read_text(encoding="utf-8"))
    scope_map = [
        json.loads(line)
        for line in (data_dir / "evaluation_scope_map.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if review.get("manifest_hash") != manifest.get("manifest_hash"):
        raise SystemExit("预检失败:assignment_review 绑定的 manifest_hash 与 dataset_manifest 不一致")
    stored = manifest.pop("manifest_hash")
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != stored:
        raise SystemExit("预检失败:dataset_manifest 内容与 manifest_hash 不符,清单被改动")
    manifest["manifest_hash"] = stored
    return manifest, review, scope_map


def validate(data_dir: Path) -> int:
    """预检:文件存在性与哈希、归属审核、scope map 审核状态。"""
    manifest, review, scope_map = load_materials(data_dir)
    problems: list[str] = []

    confirmed_keys = {
        item["document_key"] for item in review.get("attributions", []) if item.get("review_status") == "confirmed"
    }
    if review.get("status") != "confirmed":
        problems.append("assignment_review 尚未 confirmed")
    manifest_files = {item["document_key"]: item for item in manifest.get("files", [])}
    for key, item in manifest_files.items():
        source = data_dir / item["relative_path"]
        if not source.is_file():
            problems.append(f"源文件缺失: {item['relative_path']}")
            continue
        if _sha256(source) != item["sha256"]:
            problems.append(f"源文件哈希不符: {item['relative_path']}")
        if key not in confirmed_keys:
            problems.append(f"归属未确认: {key}")

    scope_status = {line["source_session"]: line["review_status"] for line in scope_map}
    unconfirmed = [sid for sid, status in scope_status.items() if status != "confirmed"]
    if unconfirmed:
        problems.append(f"scope map 存在未确认 session: {len(unconfirmed)} 条")
    if review.get("scope_map_review", {}).get("sha256") is None:
        problems.append("scope map 缺少审核哈希绑定(scope_map_review)")

    print(f"数据集: {manifest.get('dataset_key')}  manifest_hash: {manifest.get('manifest_hash')[:16]}...")
    print(f"患者: {len(manifest.get('patients', []))}  文件: {len(manifest_files)}  session: {len(scope_map)}")
    if problems:
        print(f"预检失败,{len(problems)} 项问题:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("预检通过:清单、归属审核与 scope map 全部一致。")
    return 0


async def _seed(data_dir: Path, operator_uid: str, limit: int | None, patients_filter: list[str] | None) -> int:
    """按患者分组创建导入批次;真实解析与发布由 Durable Task worker 完成。"""
    from dotenv import load_dotenv

    from yuxi.services.patient_record_ingest_service import seed_patient_files
    from yuxi.storage.postgres.manager import pg_manager

    load_dotenv(APP_ROOT / ".env", override=False)

    manifest, _review, _scope = load_materials(data_dir)
    files_by_patient: dict[str, list[dict]] = {}
    for item in manifest.get("files", []):
        source = data_dir / item["relative_path"]
        files_by_patient.setdefault(item["patient_key"], []).append(
            {"filename": item["original_filename"], "path": source}
        )

    pg_manager.initialize()
    patients = sorted(files_by_patient)
    if patients_filter:
        unknown = [key for key in patients_filter if key not in files_by_patient]
        if unknown:
            raise SystemExit(f"预检失败:清单外的患者键: {unknown}")
        patients = sorted(set(patients_filter))
    if limit:
        patients = patients[:limit]
    failures: list[str] = []
    for index, patient_key in enumerate(patients, 1):
        payload = []
        for item in files_by_patient[patient_key]:
            payload.append({"filename": item["filename"], "data": item["path"].read_bytes()})
        async with pg_manager.get_async_session_context() as db:
            try:
                batch = await seed_patient_files(
                    operator_uid=operator_uid,
                    patient_key=patient_key,
                    files=payload,
                    dataset_key=str(manifest.get("dataset_key")),
                    manifest_hash=str(manifest.get("manifest_hash")),
                    db=db,
                )
                print(f"[{index}/{len(patients)}] {patient_key}: batch={batch['id']} status={batch['status']}")
            except Exception as exc:
                failures.append(f"{patient_key}: {exc}")
                print(f"[{index}/{len(patients)}] {patient_key}: 失败 {exc}")
    if failures:
        print(f"装机完成,但有 {len(failures)} 名患者失败;失败批次不声明数据集就绪。")
        return 1
    print(f"装机批次全部创建:{len(patients)} 名患者;等待 worker 解析、审核与发布。")
    return 0


def main() -> None:
    """CLI 入口:validate 只读预检;seed 创建导入批次。"""
    parser = argparse.ArgumentParser(description="ClinEvidence 患者病历种子装机")
    parser.add_argument("command", choices=["validate", "seed"])
    parser.add_argument("--data-dir", required=True, help="装机材料与源文件目录")
    parser.add_argument("--operator-uid", help="种子操作员 UID(需在 YUXI_SEED_OPERATOR_UIDS 白名单)")
    parser.add_argument("--limit", type=int, default=None, help="仅导入前 N 名患者(演练用)")
    parser.add_argument(
        "--patients",
        default="",
        help="逗号分隔的患者键过滤;为空导入全部(评测装机按 RAGAS 清单过滤)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if args.command == "validate":
        raise SystemExit(validate(data_dir))
    if not args.operator_uid:
        raise SystemExit("seed 需要 --operator-uid")
    patients_filter = [key.strip() for key in args.patients.split(",") if key.strip()] or None
    raise SystemExit(asyncio.run(_seed(data_dir, args.operator_uid, args.limit, patients_filter)))


if __name__ == "__main__":
    main()
