"""Pydantic request/response models for the Duck Agent dashboard web server.

Extracted verbatim from ``hermes_cli/web_server.py`` (pure schema move).
``web_server`` re-exports every name here, so existing imports like
``from hermes_cli.web_server import ConfigUpdate`` keep working.
"""
from __future__ import annotations
import math
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, SecretStr, field_validator

class ConfigUpdate(BaseModel):
    config: dict
    profile: Optional[str] = None

class EnvVarUpdate(BaseModel):
    key: str
    value: str
    profile: Optional[str] = None
    api_key: str = ''

class EnvVarDelete(BaseModel):
    key: str
    profile: Optional[str] = None

class EnvVarReveal(BaseModel):
    key: str
    profile: Optional[str] = None

class MemoryProviderConfigUpdate(BaseModel):
    values: Dict[str, Any] = {}

class MemoryProviderSetupRequest(BaseModel):
    values: Dict[str, Any] = {}

class CustomEndpointUpdate(BaseModel):
    id: str = ''
    name: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    context_length: Optional[int] = None
    discover_models: bool = True
    make_default: bool = False
    models: Optional[List[str]] = None

class MessagingPlatformUpdate(BaseModel):
    enabled: Optional[bool] = None
    env: Dict[str, str] = {}
    clear_env: List[str] = []
    profile: Optional[str] = None

class TelegramOnboardingStart(BaseModel):
    bot_name: Optional[str] = None

class TelegramOnboardingApply(BaseModel):
    allowed_user_ids: List[str]
    profile: Optional[str] = None

class WhatsAppOnboardingStart(BaseModel):
    mode: Optional[str] = 'bot'
    allowed_users: Optional[str] = ''
    profile: Optional[str] = None

class WhatsAppOnboardingApply(BaseModel):
    mode: Optional[str] = None
    allowed_users: Optional[str] = None
    profile: Optional[str] = None

class AudioTranscriptionRequest(BaseModel):
    data_url: str
    mime_type: Optional[str] = None

class ManagedFileUpload(BaseModel):
    path: str
    data_url: str
    overwrite: bool = True

class ChatImageUpload(BaseModel):
    data_url: str
    filename: Optional[str] = None

class ManagedDirectoryCreate(BaseModel):
    path: str

class ManagedFileDelete(BaseModel):
    path: str
    recursive: bool = False

class ModelAssignment(BaseModel):
    """Payload for POST /api/model/set — assign a provider/model to a slot.

    scope="main"        → writes model.provider + model.default
    scope="auxiliary"   → writes auxiliary.<task>.provider + auxiliary.<task>.model
    scope="auxiliary" with task=""  → applied to every auxiliary.* slot
    scope="auxiliary" with task="__reset__"  → resets every slot to provider="auto"
    """
    scope: str
    provider: str
    model: str
    task: str = ''
    base_url: str = ''
    api_key: str = ''
    confirm_expensive_model: bool = False
    profile: Optional[str] = None

class MoaModelSlot(BaseModel):
    provider: str = ''
    model: str = ''
    reasoning_effort: Optional[str] = None
    enabled: bool = True

class _MoaReferenceControls(BaseModel):
    reference_timeout: Optional[float] = None
    degraded_reference_policy: Literal['loud', 'silent'] = 'loud'

    @field_validator('reference_timeout', mode='before')
    @classmethod
    def _validate_reference_timeout(cls, value: Any) -> Optional[float]:
        """Reject JSON booleans/non-finite values before float coercion."""
        if value is None or value == '':
            return None
        if isinstance(value, bool):
            raise ValueError('reference_timeout must be a finite positive number')
        try:
            timeout = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('reference_timeout must be a finite positive number') from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('reference_timeout must be a finite positive number')
        return timeout

class MoaPresetPayload(_MoaReferenceControls):
    reference_models: list[MoaModelSlot] = []
    aggregator: MoaModelSlot = MoaModelSlot()
    reference_temperature: Optional[float] = None
    aggregator_temperature: Optional[float] = None
    max_tokens: int = 4096
    reference_max_tokens: Optional[int] = None
    fanout: Optional[str] = None
    enabled: bool = True

class MoaConfigPayload(_MoaReferenceControls):
    default_preset: str = 'default'
    active_preset: str = ''
    presets: dict[str, MoaPresetPayload] = {}
    reference_models: list[MoaModelSlot] = []
    aggregator: MoaModelSlot = MoaModelSlot()
    reference_temperature: Optional[float] = None
    aggregator_temperature: Optional[float] = None
    max_tokens: int = 4096
    reference_max_tokens: Optional[int] = None
    fanout: Optional[str] = None
    enabled: bool = True
    profile: Optional[str] = None

class FsWriteText(BaseModel):
    path: str
    content: str

class GitPathBody(BaseModel):
    path: str

class GitFileBody(BaseModel):
    path: str
    file: Optional[str] = None

class GitCommitBody(BaseModel):
    path: str
    message: str
    push: bool = False

class GitWorktreeAddBody(BaseModel):
    path: str
    name: Optional[str] = None
    branch: Optional[str] = None
    base: Optional[str] = None
    existingBranch: Optional[str] = None

class GitWorktreeRemoveBody(BaseModel):
    path: str
    worktreePath: str
    force: bool = False

class GitBranchSwitchBody(BaseModel):
    path: str
    branch: str

class CuratorPause(BaseModel):
    paused: bool

class LearningNodeRef(BaseModel):
    id: str
    profile: Optional[str] = None

class LearningNodeEdit(BaseModel):
    id: str
    content: str
    profile: Optional[str] = None

class DebugShareRequest(BaseModel):
    redact: bool = True
    lines: int = 200

class TTSSpeakRequest(BaseModel):
    text: str

class OAuthSubmitBody(BaseModel):
    session_id: str
    code: str

class BulkDeleteSessions(BaseModel):
    ids: List[str]
    profile: Optional[str] = None

class SessionImport(BaseModel):
    sessions: List[Dict[str, Any]]
    profile: Optional[str] = None

class SessionRename(BaseModel):
    title: Optional[str] = None
    archived: Optional[bool] = None
    pinned: Optional[bool] = None
    profile: Optional[str] = None

class SessionPrune(BaseModel):
    older_than_days: Optional[float] = 90
    source: Optional[str] = None
    profile: Optional[str] = None
    started_before: Optional[float] = None
    started_after: Optional[float] = None
    title_like: Optional[str] = None
    end_reason: Optional[str] = None
    cwd_prefix: Optional[str] = None
    min_messages: Optional[int] = None
    max_messages: Optional[int] = None
    model_like: Optional[str] = None
    provider: Optional[str] = None
    user_id: Optional[str] = None
    chat_id: Optional[str] = None
    chat_type: Optional[str] = None
    branch_like: Optional[str] = None
    min_tokens: Optional[int] = None
    max_tokens: Optional[int] = None
    min_cost: Optional[float] = None
    max_cost: Optional[float] = None
    min_tool_calls: Optional[int] = None
    max_tool_calls: Optional[int] = None
    include_archived: bool = False
    dry_run: bool = False

class CronJobCreate(BaseModel):
    prompt: str = ''
    schedule: str
    name: str = ''
    deliver: str = 'local'
    skills: Optional[List[str]] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    script: Optional[str] = None
    context_from: Optional[Any] = None
    enabled_toolsets: Optional[List[str]] = None
    workdir: Optional[str] = None
    no_agent: bool = False

class CronJobUpdate(BaseModel):
    updates: dict

class AutomationBlueprintInstantiate(BaseModel):
    blueprint: str
    values: Dict[str, Any] = {}

class MCPServerCreate(BaseModel):
    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: List[str] = []
    env: Dict[str, str] = {}
    auth: Optional[str] = None
    bearer_token: Optional[SecretStr] = None
    profile: Optional[str] = None

class MCPServersReplace(BaseModel):
    servers: Dict[str, Dict[str, Any]] = {}
    profile: Optional[str] = None

class MCPEnabledToggle(BaseModel):
    enabled: bool
    profile: Optional[str] = None

class MCPCatalogInstall(BaseModel):
    name: str
    env: Dict[str, str] = {}
    enable: bool = True
    profile: Optional[str] = None

class PairingApprove(BaseModel):
    platform: str
    code: str = ''
    request_id: str = ''
    profile: Optional[str] = None

class PairingRevoke(BaseModel):
    platform: str
    user_id: str
    profile: Optional[str] = None

class WebhookCreate(BaseModel):
    name: str
    description: Optional[str] = None
    events: List[str] = []
    prompt: Optional[str] = None
    script: Optional[str] = None
    skills: List[str] = []
    deliver: str = 'log'
    deliver_only: bool = False
    deliver_chat_id: Optional[str] = None
    secret: Optional[str] = None

class WebhookEnabledToggle(BaseModel):
    enabled: bool

class CredentialPoolAdd(BaseModel):
    provider: str
    api_key: str
    label: Optional[str] = None

class MemoryProviderSelect(BaseModel):
    provider: str

class MemoryReset(BaseModel):
    target: str = 'all'

class BackupRequest(BaseModel):
    output: Optional[str] = None

class ImportRequest(BaseModel):
    archive: str
    force: bool = False

class HookCreate(BaseModel):
    event: str
    command: str
    matcher: Optional[str] = None
    timeout: Optional[int] = None
    approve: bool = True

class HookDelete(BaseModel):
    event: str
    command: str

class SkillInstallRequest(BaseModel):
    identifier: str
    profile: Optional[str] = None

class SkillUninstallRequest(BaseModel):
    name: str
    profile: Optional[str] = None

class SkillsUpdateRequest(BaseModel):
    profile: Optional[str] = None

class ProfileCreate(BaseModel):
    name: str
    clone_from: Optional[str] = None
    clone_from_default: bool = False
    clone_all: bool = False
    no_skills: bool = False
    description: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    mcp_servers: List['MCPServerCreate'] = []
    keep_skills: List[str] = []
    hub_skills: List[str] = []

class ProfileRename(BaseModel):
    new_name: str

class ProfileExport(BaseModel):
    extra_files: Dict[str, str] = {}
    output: str = ''

class ProfileImport(BaseModel):
    archive: str
    name: Optional[str] = None

class ProfileSoulUpdate(BaseModel):
    content: str

class ProfileActiveUpdate(BaseModel):
    name: str

class ProfileDescriptionUpdate(BaseModel):
    description: str = ''

class ProfileModelUpdate(BaseModel):
    provider: str
    model: str

class ProfileDescribeAuto(BaseModel):
    overwrite: bool = False

class SkillToggle(BaseModel):
    name: str
    enabled: bool
    profile: Optional[str] = None

class SkillCreate(BaseModel):
    name: str
    content: str
    category: Optional[str] = None
    profile: Optional[str] = None

class SkillContentUpdate(BaseModel):
    name: str
    content: str
    profile: Optional[str] = None

class ToolsetToggle(BaseModel):
    enabled: bool
    profile: Optional[str] = None

class ToolsetProviderSelect(BaseModel):
    provider: str
    capability: Optional[str] = None
    profile: Optional[str] = None

class ToolsetModelSelect(BaseModel):
    model: str
    provider: Optional[str] = None
    profile: Optional[str] = None

class ToolsetEnvUpdate(BaseModel):
    env: Dict[str, str]
    profile: Optional[str] = None

class ToolsetPostSetup(BaseModel):
    key: str
    profile: Optional[str] = None

class TerminalBackendSelect(BaseModel):
    backend: str
    profile: Optional[str] = None

class RawConfigUpdate(BaseModel):
    yaml_text: str
    profile: Optional[str] = None

class ThemeSetBody(BaseModel):
    name: str

class FontSetBody(BaseModel):
    font: str

class _AgentPluginInstallBody(BaseModel):
    identifier: str
    force: bool = False
    enable: bool = True

class _PluginProvidersPutBody(BaseModel):
    memory_provider: Optional[str] = None
    context_engine: Optional[str] = None

class _PluginVisibilityBody(BaseModel):
    hidden: bool