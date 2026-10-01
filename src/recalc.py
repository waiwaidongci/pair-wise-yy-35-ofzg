from __future__ import annotations

from typing import Any, Dict, List, Optional

from .repository import (BATCH_COMPLETED, BATCH_FAILED, BATCH_PARTIAL,
                         BATCH_RUNNING, INSTRUMENT_RESUMABLE, Repository)


class RecalcProcessor:
    """
    重算批次编排：

    - request_id 幂等：同一请求重试沿用首次结果，已完成批次直接返回冻结摘要；
    - 认领槽位跨批次互斥：并发批次遇到正在重算的仪器记 skipped，两批互不覆盖；
    - 失败 fail-fast 并保留仪器级检查点，重试只从未完成仪器恢复；
    - 单台仪器的重算在 Repository.recalc_instrument 的单事务内完成。
    """

    def __init__(self, repository: Repository):
        self.repository = repository

    def before_instrument(self, batch_id: int, instrument_id: int) -> None:
        """测试可覆盖的故障注入钩子：在认领成功后、重算事务前抛出。"""

    def submit_or_get(self, request_id: str, instrument_ids: List[int],
                      window_from: Optional[str], window_to: Optional[str],
                      reason: str, actor: str) -> Dict[str, Any]:
        existing = self.repository.get_batch_by_request(request_id)
        if existing is not None:
            if existing["status"] in (BATCH_COMPLETED, BATCH_FAILED, BATCH_PARTIAL):
                if existing["status"] == BATCH_COMPLETED:
                    # 同一请求重试沿用首次结果
                    return existing
            # running/失败/部分：沿同一请求继续，从最后未完成仪器恢复
            return self._run(self.repository.get_batch(existing["id"]), actor)
        batch = self.repository.create_batch(
            request_id, instrument_ids, window_from, window_to, reason, actor)
        return self._run(batch, actor)

    def resume_batch(self, batch_id: int, actor: str) -> Dict[str, Any]:
        batch = self.repository.get_batch(batch_id)
        if batch["status"] == BATCH_COMPLETED:
            return batch
        return self._run(batch, actor)

    def _run(self, batch: Dict[str, Any], actor: str) -> Dict[str, Any]:
        batch_id = int(batch["id"])
        lock = self.repository.batch_lock(batch_id)
        # 同一批次进程内串行；跨批次由数据库认领槽位互斥
        if not lock.acquire(blocking=False):
            return self.repository.get_batch(batch_id)
        try:
            # 每次运行都从数据库读取最新检查点，使重试能从failed/skipped仪器恢复
            rows = self.repository.list_batch_instruments(batch_id)
            summary = self.repository.get_batch(batch_id)["summary"] or {}
            instruments = summary.setdefault("instruments", {})
            had_failure = False
            had_skip = False
            for row in rows:
                instrument_id = int(row["instrument_id"])
                if row["status"] == "completed":
                    had_skip = had_skip or instruments.get(str(instrument_id), {}).get("skipped", 0) > 0
                    continue
                if not self.repository.claim_instrument(batch_id, instrument_id):
                    self.repository.mark_instrument(
                        batch_id, instrument_id, "skipped",
                        "该仪器正被其它重算批次处理，本批次跳过，避免互相覆盖")
                    instruments[str(instrument_id)] = {"scanned": 0, "revised": 0,
                                                       "unchanged": 0, "skipped": 1}
                    had_skip = True
                    self.repository.save_batch_result(batch_id, BATCH_RUNNING, summary)
                    continue
                try:
                    self.before_instrument(batch_id, instrument_id)
                    stats = self.repository.recalc_instrument(
                        batch_id, instrument_id, batch["window_from"],
                        batch["window_to"], actor)
                    self.repository.mark_instrument(
                        batch_id, instrument_id, "completed",
                        f"扫描{stats['scanned']}，修订{stats['revised']}，跳过{stats['skipped']}")
                    instruments[str(instrument_id)] = stats
                    had_skip = had_skip or stats["skipped"] > 0
                    self.repository.save_batch_result(batch_id, BATCH_RUNNING, summary)
                except Exception as exc:  # 检查点停留在failed，后续重试从这里恢复
                    self.repository.mark_instrument(
                        batch_id, instrument_id, "failed", f"{type(exc).__name__}: {exc}")
                    instruments.setdefault(str(instrument_id), {}).update({"error": str(exc)})
                    self.repository.save_batch_result(batch_id, BATCH_FAILED, summary)
                    had_failure = True
                    break
            final = self.repository.get_batch(batch_id)
            if not had_failure:
                status = BATCH_PARTIAL if had_skip else BATCH_COMPLETED
                self.repository.save_batch_result(batch_id, status, summary)
            else:
                self.repository.save_batch_result(batch_id, BATCH_FAILED, summary)
            return self.repository.get_batch(batch_id)
        finally:
            lock.release()
