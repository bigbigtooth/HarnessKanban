"""Domain error taxonomy.

Every business failure raised inside ``domain`` services must be a ``DomainError`` subclass
so both HTTP adapters can serialize it into the frozen envelope (CONTRACT.md §3) without
guessing a status code or leaking framework/persistence details.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for all expected business failures."""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    default_message: str = "服务处理失败"

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


class InvalidArgumentError(DomainError):
    code = "INVALID_ARGUMENT"
    http_status = 400
    default_message = "请求参数不合法"


class UnsupportedParameterError(DomainError):
    code = "UNSUPPORTED_PARAMETER"
    http_status = 400
    default_message = "请求包含不支持的参数"


class IdempotencyKeyRequiredError(DomainError):
    code = "IDEMPOTENCY_KEY_REQUIRED"
    http_status = 400
    default_message = "写操作必须携带 Idempotency-Key 请求头"


class WebUiKeyRequiredError(DomainError):
    """WebUI 已启用访问密钥，但请求没有携带（cookie 或 X-WebUI-Key 头）。"""

    code = "WEBUI_KEY_REQUIRED"
    http_status = 403
    default_message = "缺少 WebUI 访问密钥"


class WebUiKeyInvalidError(DomainError):
    """WebUI 访问密钥不正确（或密钥已轮换，旧 cookie 失效）。"""

    code = "WEBUI_KEY_INVALID"
    http_status = 403
    default_message = "WebUI 访问密钥不正确"


class AgentTokenMissingError(DomainError):
    code = "AGENT_TOKEN_MISSING"
    http_status = 401
    default_message = "缺少 AGENT_TOKEN 请求头"


class AgentTokenInvalidError(DomainError):
    code = "AGENT_TOKEN_INVALID"
    http_status = 401
    default_message = "AGENT_TOKEN 无效"


class AgentDisabledError(DomainError):
    code = "AGENT_DISABLED"
    http_status = 401
    default_message = "Agent 已被禁用"


class RoleForbiddenError(DomainError):
    code = "ROLE_FORBIDDEN"
    http_status = 403
    default_message = "当前身份无权执行该操作"


class SelfReviewForbiddenError(DomainError):
    code = "SELF_REVIEW_FORBIDDEN"
    http_status = 403
    default_message = "禁止审核自己提交的结果"


class RequestOriginForbiddenError(DomainError):
    code = "REQUEST_ORIGIN_FORBIDDEN"
    http_status = 403
    default_message = "请求来源不在允许范围内"


class ResourceNotFoundError(DomainError):
    code = "RESOURCE_NOT_FOUND"
    http_status = 404
    default_message = "资源不存在或不可访问"


class MethodNotAllowedError(DomainError):
    code = "METHOD_NOT_ALLOWED"
    http_status = 405
    default_message = "请求方法不被支持"


class VersionConflictError(DomainError):
    code = "VERSION_CONFLICT"
    http_status = 409
    default_message = "资源版本已变化，请重新读取当前状态"


class TaskAlreadyClaimedError(DomainError):
    code = "TASK_ALREADY_CLAIMED"
    http_status = 409
    default_message = "任务已被其他 Agent 领取，请刷新任务列表"


class TaskDifficultyMismatchError(DomainError):
    code = "TASK_DIFFICULTY_MISMATCH"
    http_status = 409
    default_message = "指定任务与本次等级或项目范围不匹配"


class InvalidTransitionError(DomainError):
    code = "INVALID_TRANSITION"
    http_status = 409
    default_message = "当前状态不允许该操作"


class ExecutionTimeoutError(DomainError):
    code = "EXECUTION_TIMEOUT"
    http_status = 409
    default_message = "当前执行已超过固定截止时刻"


class ExecutionNotCurrentError(DomainError):
    code = "EXECUTION_NOT_CURRENT"
    http_status = 409
    default_message = "执行 ID 不是当前有效执行"


class CapacityExceededError(DomainError):
    code = "CAPACITY_EXCEEDED"
    http_status = 409
    default_message = "Agent 可用容量不足"


class CapacityBelowReservedError(DomainError):
    code = "CAPACITY_BELOW_RESERVED"
    http_status = 409
    default_message = "最大并发不能低于当前未释放容量槽数"


class IdempotencyKeyReusedError(DomainError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409
    default_message = "同一幂等键被用于不同的请求参数"


class IdempotencyInProgressError(DomainError):
    code = "IDEMPOTENCY_IN_PROGRESS"
    http_status = 409
    default_message = "相同幂等键的请求正在处理中，请稍后按原键重试"


class ReviewAlreadyDecidedError(DomainError):
    code = "REVIEW_ALREADY_DECIDED"
    http_status = 409
    default_message = "该提交已经完成审核"


class SubmissionExistsError(DomainError):
    code = "SUBMISSION_EXISTS"
    http_status = 409
    default_message = "该执行已存在正式提交"


class AgentNameConflictError(DomainError):
    code = "AGENT_NAME_CONFLICT"
    http_status = 409
    default_message = "Agent 名称已存在"


class MembershipConflictError(DomainError):
    code = "MEMBERSHIP_CONFLICT"
    http_status = 409
    default_message = "成员关系已存在或状态冲突"


class LastSupervisorRequiredError(DomainError):
    code = "LAST_SUPERVISOR_REQUIRED"
    http_status = 409
    default_message = "项目必须保留至少一名有效主管"


class ProjectNotActiveError(DomainError):
    code = "PROJECT_NOT_ACTIVE"
    http_status = 409
    default_message = "项目当前生命周期不允许该操作"


class TaskNumberConflictError(DomainError):
    code = "TASK_NUMBER_CONFLICT"
    http_status = 409
    default_message = "任务编号分配冲突，请重试"


class AttachmentLimitExceededError(DomainError):
    code = "ATTACHMENT_LIMIT_EXCEEDED"
    http_status = 409
    default_message = "附件数量超过限制"


class DocumentConflictError(DomainError):
    code = "DOCUMENT_CONFLICT"
    http_status = 409
    default_message = "同名文档已存在，请明确选择新版本或新文档"


class FileTooLargeError(DomainError):
    code = "FILE_TOO_LARGE"
    http_status = 413
    default_message = "文件超过大小限制"


class UnsupportedMediaTypeError(DomainError):
    code = "UNSUPPORTED_MEDIA_TYPE"
    http_status = 415
    default_message = "不支持的文件类型"


class RateLimitedError(DomainError):
    code = "RATE_LIMITED"
    http_status = 429
    default_message = "请求过于频繁，请按 Retry-After 退避"


class DatabaseBusyError(DomainError):
    code = "DATABASE_BUSY"
    http_status = 503
    default_message = "数据库繁忙，请按原幂等键稍后重试"


class ClaimLockTimeoutError(DomainError):
    code = "CLAIM_LOCK_TIMEOUT"
    http_status = 503
    default_message = "等待全局领取锁超时，请按原幂等键重试"


class ClaimLockMissingError(DomainError):
    code = "CLAIM_LOCK_MISSING"
    http_status = 503
    default_message = "全局领取锁行缺失或不可用，拒绝无锁执行"


class DependencyUnavailableError(DomainError):
    code = "DEPENDENCY_UNAVAILABLE"
    http_status = 503
    default_message = "依赖服务不可用"


class OssUnavailableError(DomainError):
    code = "OSS_UNAVAILABLE"
    http_status = 503
    default_message = "对象存储暂时不可用，请按原幂等键稍后重试"


# Transaction-control signals. These are not HTTP responses: adapters catch them to
# restart a fresh transaction (CONTRACT.md §8), never to build a response body.
class RetryTransaction(Exception):
    """Raised to force a full rollback and re-plan/retry (lock plan invalidated)."""


class IdempotencyRaceLost(Exception):
    """Another transaction won the same idempotency key; read its receipt in a new txn."""

    def __init__(self, actor_scope: str, operation: str, key: str) -> None:
        self.actor_scope = actor_scope
        self.operation = operation
        self.key = key
        super().__init__("idempotency reservation lost")
