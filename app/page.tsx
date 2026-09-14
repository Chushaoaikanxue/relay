'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  AlertCircle,
  ArrowRightLeft,
  ArrowUpRight,
  CheckCircle2,
  CircleHelp,
  Database,
  FileArchive,
  FileCode2,
  FolderOpen,
  Gauge,
  History,
  LayoutDashboard,
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  Menu,
  Network,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  ServerCog,
  Settings2,
  ShieldCheck,
  TerminalSquare,
  Trash2,
  UploadCloud,
  UserRound,
  X,
  Zap,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';

const API_BASE = '/relay/api';

type TaskStatus = 'transferring' | 'queued' | 'completed' | 'failed' | 'paused';
type TaskNodeDetails = { name: string; host: string; ssh_port: number; transfer_host?: string | null; transfer_port?: number | null };
type Task = {
  id: string;
  name: string;
  source: string;
  destination: string;
  size: string;
  transferred: string;
  progress: number;
  status: TaskStatus;
  speed: string;
  eta: string;
  files: string;
  started: string;
  updated: string;
  kind: 'folder' | 'archive' | 'code';
  error?: string | null;
  log: string;
  source_node_id?: string | null;
  destination_node_id?: string | null;
  source_path?: string | null;
  destination_path?: string | null;
  delete_enabled?: boolean;
  bandwidth_limit_kbps?: number | null;
  max_size_bytes?: number | null;
  schedule_at?: string | null;
  scheduled?: boolean;
  source_node?: TaskNodeDetails | null;
  destination_node?: TaskNodeDetails | null;
  direct_host?: string | null;
  direct_port?: number | null;
  verify_after_transfer?: boolean;
  verification_status?: 'running' | 'passed' | 'failed' | null;
  verification_message?: string | null;
  owner_username?: string | null;
  owner_display_name?: string | null;
};

type NewTaskPayload = {
  name: string;
  source_node_id: string;
  source_path: string;
  destination_node_id: string;
  destination_path: string;
  bandwidth_limit_mbps: number;
  max_size_gb: number;
  delete_enabled: boolean;
  schedule_at?: string;
  direct_host?: string;
  direct_port?: number;
  verify_after_transfer: boolean;
};

type User = { id: number; username: string; display_name: string; role: 'admin' | 'user' };
type TransferSettings = { max_concurrent: number; max_per_node: number };
type Node = {
  id: string;
  name: string;
  status: 'online' | 'offline' | 'pending';
  host: string;
  ssh_port: number;
  username: string;
  fingerprint?: string | null;
  auth_type?: 'key' | 'password';
  last_error?: string | null;
  last_seen_at?: string | null;
  created_at: string;
};
type Bootstrap = { setup_required: boolean; user: User | null };
type AuthState = { status: 'loading' } | { status: 'setup' } | { status: 'login' } | { status: 'ready'; user: User } | { status: 'unavailable'; message: string };

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set('Accept', 'application/json');
  if (options.body) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET') headers.set('X-Relay-Request', '1');
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers, credentials: 'same-origin' });
  const payload: unknown = await response.json().catch(() => ({ error: '服务返回内容不正确' }));
  if (!response.ok) {
    const error = typeof payload === 'object' && payload !== null && 'error' in payload && typeof payload.error === 'string'
      ? payload.error
      : '请求失败';
    throw new ApiError(error, response.status);
  }
  return payload as T;
}

function statusLabel(status: TaskStatus, scheduled = false, verificationStatus?: Task['verification_status']) {
  if (scheduled) return '已预约';
  if (verificationStatus === 'running') return '校验中';
  return { transferring: '传输中', queued: '等待执行', completed: '已完成', failed: '失败', paused: '已暂停' }[status];
}

function displayTime(value?: string | null) {
  if (!value || value === '—') return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).format(date);
}

function scheduleLabel(task: Task) {
  return displayTime(task.scheduled && task.schedule_at ? task.schedule_at : task.started);
}

function statusClass(status: TaskStatus) {
  return { transferring: 'status-live', queued: 'status-queued', completed: 'status-complete', failed: 'status-failed', paused: 'status-paused' }[status];
}

function sizeLimitLabel(value?: number | null) {
  return value ? `${Math.round(value / 1024 / 1024 / 1024)} GB` : '不限';
}

function initials(name: string) {
  const parts = name.trim().split(/\s+/);
  return parts.length > 1 ? `${parts[0][0]}${parts.at(-1)?.[0]}`.toUpperCase() : name.slice(0, 2).toUpperCase();
}

function TaskIcon({ kind }: { kind: Task['kind'] }) {
  const Icon = kind === 'archive' ? FileArchive : kind === 'code' ? FileCode2 : FolderOpen;
  return <Icon strokeWidth={1.8} />;
}

function MiniChart({ hasData }: { hasData: boolean }) {
  return (
    <div className={`mini-chart ${hasData ? '' : 'chart-empty'}`} aria-label="过去 7 天吞吐量趋势">
      <svg viewBox="0 0 260 72" aria-hidden="true" preserveAspectRatio="none">
        <defs><linearGradient id="chart-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="#8df2c8" stopOpacity="0.32" /><stop offset="100%" stopColor="#8df2c8" stopOpacity="0" /></linearGradient></defs>
        <path d="M0 61 C20 53, 26 58, 44 43 S69 45, 85 49 S108 25, 127 34 S151 23, 167 28 S191 13, 210 21 S235 11, 260 15 L260 72 L0 72Z" fill="url(#chart-fill)" />
        <path d="M0 61 C20 53, 26 58, 44 43 S69 45, 85 49 S108 25, 127 34 S151 23, 167 28 S191 13, 210 21 S235 11, 260 15" fill="none" stroke="#8df2c8" strokeWidth="2.5" strokeLinecap="round" />
      </svg>
      <div className="chart-labels"><span>周一</span><span>周三</span><span>周五</span><span>今天</span></div>
    </div>
  );
}

function AuthScreen({ mode, onAuthenticated }: { mode: 'setup' | 'login'; onAuthenticated: (user: User) => void }) {
  const [username, setUsername] = useState(mode === 'setup' ? 'admin' : '');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true); setError('');
    try {
      const payload = mode === 'setup' ? { username, display_name: displayName, password } : { username, password };
      const data = await api<{ user: User }>(mode === 'setup' ? '/setup' : '/login', { method: 'POST', body: JSON.stringify(payload) });
      onAuthenticated(data.user);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-brand-panel">
        <div className="auth-brand"><div className="brand-mark"><Zap size={18} fill="currentColor" /></div><span>relay</span></div>
        <div className="auth-copy"><p className="eyebrow"><span className="eyebrow-dot" />安全传输控制台</p><h1>让跨节点传输<br />变得可见、可控。</h1><p>任务、进度、日志与服务器节点，都集中在一个工作台中。</p></div>
        <div className="auth-feature-list"><div><Network /><span><strong>节点隔离</strong>网页服务不持有 root 权限</span></div><div><Database /><span><strong>状态持久化</strong>任务和历史保存在服务器</span></div><div><ShieldCheck /><span><strong>安全会话</strong>管理页面必须登录后访问</span></div></div>
      </section>
      <section className="auth-form-panel">
        <form className="auth-card" onSubmit={submit}>
          <div className="auth-card-icon">{mode === 'setup' ? <UserRound /> : <LockKeyhole />}</div>
          <h2>{mode === 'setup' ? '初始化管理员' : '欢迎回来'}</h2>
          <p>{mode === 'setup' ? '这是首次设置。创建的账户将拥有工作台管理权限。' : '登录后继续管理传输任务。'}</p>
          <div className="auth-fields">
            {mode === 'setup' && <label>显示名称<Input autoComplete="name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="例如：管理员" required /></label>}
            <label>用户名<Input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="admin" required /></label>
            <label>密码<Input type="password" autoComplete={mode === 'setup' ? 'new-password' : 'current-password'} value={password} onChange={(event) => setPassword(event.target.value)} placeholder={mode === 'setup' ? '至少 12 个字符' : '输入登录密码'} minLength={mode === 'setup' ? 12 : undefined} required /></label>
          </div>
          {error && <div className="auth-error"><AlertCircle />{error}</div>}
          <Button type="submit" className="auth-submit" disabled={submitting}>{submitting ? <LoaderCircle className="spin" /> : mode === 'setup' ? <ShieldCheck /> : <LockKeyhole />}{submitting ? '处理中...' : mode === 'setup' ? '创建管理员并进入' : '登录'}</Button>
          <div className="auth-note"><ShieldCheck />会话使用安全 Cookie，密码只以加密摘要保存。</div>
        </form>
      </section>
    </main>
  );
}

function ServiceUnavailable({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <main className="status-screen"><div className="status-card"><div className="status-icon failed"><AlertCircle /></div><h1>控制服务暂时不可用</h1><p>{message}</p><Button onClick={onRetry}><RefreshCw />重新连接</Button></div></main>;
}

function NewTransferDialog({ nodes, onCreate }: { nodes: Node[]; onCreate: (payload: NewTaskPayload) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [sourceNodeId, setSourceNodeId] = useState('');
  const [destinationNodeId, setDestinationNodeId] = useState('');
  const [sourcePath, setSourcePath] = useState('');
  const [destinationPath, setDestinationPath] = useState('');
  const [bandwidthLimit, setBandwidthLimit] = useState('0');
  const [sizeLimit, setSizeLimit] = useState('100');
  const [deleteEnabled, setDeleteEnabled] = useState(false);
  const [scheduleAt, setScheduleAt] = useState('');
  const [directHost, setDirectHost] = useState('');
  const [directPort, setDirectPort] = useState('');
  const [verifyAfterTransfer, setVerifyAfterTransfer] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const onlineNodes = useMemo(() => nodes.filter((node) => node.status === 'online'), [nodes]);

  useEffect(() => {
    if (!open || onlineNodes.length < 2) return;
    const validSource = onlineNodes.some((node) => node.id === sourceNodeId) ? sourceNodeId : onlineNodes[0].id;
    const validDestination = onlineNodes.some((node) => node.id === destinationNodeId && node.id !== validSource)
      ? destinationNodeId
      : onlineNodes.find((node) => node.id !== validSource)?.id || '';
    if (sourceNodeId !== validSource) setSourceNodeId(validSource);
    if (destinationNodeId !== validDestination) setDestinationNodeId(validDestination);
  }, [open, onlineNodes, sourceNodeId, destinationNodeId]);

  function chooseSourceNode(nextId: string) {
    setSourceNodeId(nextId);
    if (nextId === destinationNodeId) setDestinationNodeId(onlineNodes.find((node) => node.id !== nextId)?.id || '');
  }

  function chooseDestinationNode(nextId: string) {
    setDestinationNodeId(nextId);
    if (nextId === sourceNodeId) setSourceNodeId(onlineNodes.find((node) => node.id !== nextId)?.id || '');
  }

  async function createTask() {
    if (!sourceNodeId || !destinationNodeId || !sourcePath || !destinationPath || !sizeLimit.trim() || sourceNodeId === destinationNodeId) return;
    setSubmitting(true); setError('');
    try {
      await onCreate({
        name,
        source_node_id: sourceNodeId,
        source_path: sourcePath,
        destination_node_id: destinationNodeId,
        destination_path: destinationPath,
        bandwidth_limit_mbps: Number(bandwidthLimit || 0),
        max_size_gb: Number(sizeLimit),
        delete_enabled: deleteEnabled,
        schedule_at: scheduleAt ? new Date(scheduleAt).toISOString() : '',
        direct_host: directHost,
        direct_port: directPort.trim() ? Number(directPort) : undefined,
        verify_after_transfer: verifyAfterTransfer,
      });
      setName(''); setSourcePath(''); setDestinationPath(''); setSizeLimit('100'); setDeleteEnabled(false); setVerifyAfterTransfer(false); setScheduleAt(''); setDirectHost(''); setDirectPort(''); setOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger disabled={onlineNodes.length < 2} render={<Button className="new-task-button" title={onlineNodes.length < 2 ? '至少需要两台在线节点' : undefined}><Plus />新建传输</Button>} />
      <DialogContent className="transfer-dialog direct-transfer-dialog">
        <DialogHeader><div className="dialog-icon"><UploadCloud size={20} /></div><DialogTitle>新建节点直传任务</DialogTitle><DialogDescription>Relay 只下发任务和接收进度，文件或目录会从源节点直接流向目标节点。</DialogDescription></DialogHeader>
        <div className="form-stack">
          <label className="task-name-field" htmlFor="task-name" aria-label="任务名称"><span className="field-label"><strong>任务名称</strong><em>可选</em></span><Input id="task-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：media-archive-aug-31" /></label>
          <div className="transfer-route">
            <label className="route-source-node">源节点<NativeSelect className="node-native-select" value={sourceNodeId} onChange={(event) => chooseSourceNode(event.target.value)}>{onlineNodes.map((node) => <NativeSelectOption key={node.id} value={node.id} disabled={node.id === destinationNodeId}>{node.name} · {node.host}:{node.ssh_port}</NativeSelectOption>)}</NativeSelect></label>
            <label className="route-source-path">源路径<span>可填写单个文件或目录</span><Input value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} placeholder="/mnt/data/report.zip 或 /mnt/data/source/" /></label>
            <Button type="button" variant="outline" size="icon" className="swap-route" aria-label="交换源节点和目标节点" onClick={() => { setSourceNodeId(destinationNodeId); setDestinationNodeId(sourceNodeId); setSourcePath(destinationPath); setDestinationPath(sourcePath); setDirectHost(''); setDirectPort(''); }}><ArrowRightLeft /></Button>
            <label className="route-destination-node">目标节点<NativeSelect className="node-native-select" value={destinationNodeId} onChange={(event) => chooseDestinationNode(event.target.value)}>{onlineNodes.map((node) => <NativeSelectOption key={node.id} value={node.id} disabled={node.id === sourceNodeId}>{node.name} · {node.host}:{node.ssh_port}</NativeSelectOption>)}</NativeSelect></label>
            <label className="route-destination-path">目标目录<span>写入已有目录</span><Input value={destinationPath} onChange={(event) => setDestinationPath(event.target.value)} placeholder="/mnt/data/archive/" /></label>
          </div>
          <div className="transfer-options direct-route-options">
            <label>本次直传目标 IP（可选）<span>同局域网时填写目标节点内网 IP；留空走公网</span><Input value={directHost} onChange={(event) => setDirectHost(event.target.value)} placeholder="例如：10.0.0.12" /></label>
            <label>本次直传端口（可选）<span>默认使用目标节点的 SSH 端口</span><Input type="number" min="1" max="65535" value={directPort} onChange={(event) => setDirectPort(event.target.value)} placeholder="例如：22" /></label>
          </div>
          <div className="transfer-options">
            <label>带宽上限<span>MB/s，0 表示不限速</span><Input type="number" min="0" max="10240" value={bandwidthLimit} onChange={(event) => setBandwidthLimit(event.target.value)} /></label>
            <label>任务大小上限<span>GB，默认 100；0 表示不限</span><Input type="number" min="0" max="1048576" step="1" value={sizeLimit} onChange={(event) => setSizeLimit(event.target.value)} /></label>
            <div className="switch-option"><div><strong>镜像删除（仅目录）</strong><span>删除目标中源目录没有的文件</span></div><Switch checked={deleteEnabled} onCheckedChange={setDeleteEnabled} /></div>
            <div className="switch-option"><div><strong>传输后内容校验</strong><span>完成后逐文件读取两端内容；仅校验，不改动文件</span></div><Switch checked={verifyAfterTransfer} onCheckedChange={setVerifyAfterTransfer} /></div>
          </div>
          <label>预约开始时间<span>留空则在目录和大小校验通过后立即传输</span><Input type="datetime-local" value={scheduleAt} onChange={(event) => setScheduleAt(event.target.value)} /></label>
          <div className="path-hint">源路径可填单个文件或目录：单个文件会保留原文件名；目录以 <code>/</code> 结尾时传目录内容，不以 <code>/</code> 结尾时会保留源目录名。</div>
          <div className="transport-note"><ShieldCheck size={16} /><span>每个任务使用短期受限密钥，目标节点只允许写入本次选择的目录。</span></div>
          {error && <div className="dialog-error"><AlertCircle />{error}</div>}
        </div>
        <DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>取消</Button><Button className="dialog-primary" onClick={createTask} disabled={!sourcePath || !destinationPath || !sizeLimit.trim() || sourceNodeId === destinationNodeId || submitting}>{submitting && <LoaderCircle className="spin" />}{submitting ? '正在校验...' : scheduleAt ? '校验并预约' : '校验并开始直传'}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AddNodesDialog({ onCreate }: { onCreate: (payload: Record<string, unknown>) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [namePrefix, setNamePrefix] = useState('node');
  const [hosts, setHosts] = useState('');
  const [port, setPort] = useState('22');
  const [username, setUsername] = useState('');
  const [privateKey, setPrivateKey] = useState('');
  const [authType, setAuthType] = useState<'key' | 'password'>('key');
  const [sshPassword, setSshPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  async function createNodes() {
    if (!hosts.trim() || (authType === 'key' ? !privateKey.trim() : !sshPassword)) return;
    setSubmitting(true); setError('');
    try {
      await onCreate({ name_prefix: namePrefix, hosts, ssh_port: Number(port), username, auth_type: authType, private_key: privateKey, ssh_password: sshPassword });
      setHosts(''); setPrivateKey(''); setSshPassword(''); setOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '添加节点失败');
    } finally {
      setSubmitting(false);
    }
  }

  async function loadKeyFile(file?: File) {
    if (!file) return;
    if (file.size > 32_768) { setError('私钥文件过大'); return; }
    setPrivateKey(await file.text());
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button className="new-task-button"><Plus />添加节点</Button>} />
      <DialogContent className="transfer-dialog node-dialog">
        <DialogHeader><div className="dialog-icon"><ServerCog size={20} /></div><DialogTitle>添加 SSH 节点</DialogTitle><DialogDescription>可一次添加多台使用相同端口、用户和认证方式的服务器，系统会在后台测试连接。</DialogDescription></DialogHeader>
        <div className="node-form-grid">
          <label className="full-field">节点地址<span>每行一个 IPv4 或主机名</span><Textarea value={hosts} onChange={(event) => setHosts(event.target.value)} placeholder={'node-a.example.com\nnode-b.example.com'} rows={4} /></label>
          <label>名称前缀<Input value={namePrefix} onChange={(event) => setNamePrefix(event.target.value)} placeholder="node" /></label>
          <label>SSH 端口<Input type="number" min="1" max="65535" value={port} onChange={(event) => setPort(event.target.value)} /></label>
          <label>SSH 用户<Input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="例如：relay" /></label>
          <label>认证方式<NativeSelect value={authType} onChange={(event) => setAuthType(event.target.value as 'key' | 'password')}><NativeSelectOption value="key">SSH 私钥</NativeSelectOption><NativeSelectOption value="password">SSH 密码</NativeSelectOption></NativeSelect></label>
          {authType === 'key' ? <div className="full-field key-field"><span className="field-label">SSH 私钥</span><span className="field-help">仅支持无需输入口令的 OpenSSH 私钥；保存后不会返回浏览器</span><Textarea className="private-key-input" value={privateKey} onChange={(event) => setPrivateKey(event.target.value)} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" rows={6} /><span className="key-file-row"><label className="file-picker"><KeyRound />选择私钥文件<input type="file" accept=".pem,.key" onChange={(event) => void loadKeyFile(event.target.files?.[0])} /></label>{privateKey && <em><CheckCircle2 />已读取 {privateKey.length} 字符</em>}</span></div> : <label className="full-field">SSH 密码<span>仅用于连接此节点，保存于服务器受限目录，不会返回前端</span><Input type="password" autoComplete="new-password" value={sshPassword} onChange={(event) => setSshPassword(event.target.value)} placeholder="输入节点 SSH 登录密码" /></label>}
        </div>
        <div className="transport-note"><ShieldCheck size={16} /><span>{authType === 'key' ? '私钥以 0600 权限保存在 Relay 服务器受限目录，数据库只记录凭据名称和 SHA256 指纹。' : 'SSH 密码以 0600 权限保存在 Relay 服务器受限目录，接口和日志不会返回密码。'}</span></div>
        {error && <div className="dialog-error"><AlertCircle />{error}</div>}
        <DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>取消</Button><Button className="dialog-primary" onClick={createNodes} disabled={!hosts.trim() || (authType === 'key' ? !privateKey.trim() : !sshPassword) || submitting}>{submitting && <LoaderCircle className="spin" />}{submitting ? '正在保存...' : '保存并测试连接'}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TransferSettingsDialog({ settings, onSave }: { settings: TransferSettings; onSave: (settings: TransferSettings) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [maxConcurrent, setMaxConcurrent] = useState(String(settings.max_concurrent));
  const [maxPerNode, setMaxPerNode] = useState(String(settings.max_per_node));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (open) {
      setMaxConcurrent(String(settings.max_concurrent));
      setMaxPerNode(String(settings.max_per_node));
      setError('');
    }
  }, [open, settings]);

  async function save() {
    setSaving(true); setError('');
    try {
      await onSave({ max_concurrent: Number(maxConcurrent), max_per_node: Number(maxPerNode) });
      setOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger render={<button className="nav-item" type="button"><Settings2 size={17} /><span>传输设置</span></button>} />
    <DialogContent className="transfer-dialog settings-dialog">
      <DialogHeader><div className="dialog-icon"><Settings2 size={20} /></div><DialogTitle>传输并发设置</DialogTitle><DialogDescription>独立节点任务可并行；写入相同或包含关系的目标目录仍会自动排队。</DialogDescription></DialogHeader>
      <div className="settings-grid">
        <label><strong>全局并发任务数</strong><span>默认 4，最多 16</span><Input type="number" min="1" max="16" value={maxConcurrent} onChange={(event) => setMaxConcurrent(event.target.value)} /></label>
        <label><strong>单节点并发任务数</strong><span>默认 2，最多 8</span><Input type="number" min="1" max="8" value={maxPerNode} onChange={(event) => setMaxPerNode(event.target.value)} /></label>
      </div>
      <div className="transport-note"><ShieldCheck size={16} /><span>同一节点可同时参与多条不同目录任务；相同目标目录始终串行，避免文件冲突。</span></div>
      {error && <div className="dialog-error"><AlertCircle />{error}</div>}
      <DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>取消</Button><Button className="dialog-primary" onClick={save} disabled={saving || !maxConcurrent.trim() || !maxPerNode.trim()}>{saving && <LoaderCircle className="spin" />}{saving ? '正在保存...' : '保存设置'}</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}

function ChangePasswordDialog({ onChange }: { onChange: (currentPassword: string, newPassword: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function save() {
    setSaving(true); setError('');
    try { await onChange(currentPassword, newPassword); setCurrentPassword(''); setNewPassword(''); setOpen(false); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '修改密码失败'); }
    finally { setSaving(false); }
  }
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger render={<button className="nav-item" type="button"><KeyRound size={17} /><span>修改登录密码</span></button>} />
    <DialogContent className="transfer-dialog settings-dialog"><DialogHeader><div className="dialog-icon"><KeyRound size={20} /></div><DialogTitle>修改登录密码</DialogTitle><DialogDescription>修改后使用新密码登录；密码只以加密摘要保存。</DialogDescription></DialogHeader>
      <div className="form-stack"><label>当前密码<Input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label><label>新密码<span>至少 12 个字符</span><Input type="password" autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label></div>
      {error && <div className="dialog-error"><AlertCircle />{error}</div>}<DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>取消</Button><Button className="dialog-primary" onClick={save} disabled={saving || !currentPassword || newPassword.length < 12}>{saving && <LoaderCircle className="spin" />}保存新密码</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}

function UserManagementDialog({ users, onCreate, onReset }: { users: User[]; onCreate: (payload: { username: string; display_name: string; password: string; role: 'admin' | 'user' }) => Promise<void>; onReset: (id: number, password: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState(''); const [displayName, setDisplayName] = useState(''); const [password, setPassword] = useState(''); const [role, setRole] = useState<'admin' | 'user'>('user');
  const [resetUser, setResetUser] = useState<User | null>(null); const [resetPassword, setResetPassword] = useState(''); const [error, setError] = useState(''); const [saving, setSaving] = useState(false);
  async function create() { setSaving(true); setError(''); try { await onCreate({ username, display_name: displayName, password, role }); setUsername(''); setDisplayName(''); setPassword(''); setOpen(false); } catch (reason) { setError(reason instanceof Error ? reason.message : '创建账号失败'); } finally { setSaving(false); } }
  async function reset() { if (!resetUser) return; setSaving(true); setError(''); try { await onReset(resetUser.id, resetPassword); setResetUser(null); setResetPassword(''); } catch (reason) { setError(reason instanceof Error ? reason.message : '重置密码失败'); } finally { setSaving(false); } }
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger render={<button className="nav-item" type="button"><UserRound size={17} /><span>账号与权限</span></button>} />
    <DialogContent className="transfer-dialog users-dialog"><DialogHeader><div className="dialog-icon"><ShieldCheck size={20} /></div><DialogTitle>账号与权限</DialogTitle><DialogDescription>管理员可创建账号并重置密码；普通用户只能查看自己的任务和操作记录。</DialogDescription></DialogHeader>
      <section className="user-management-section"><div className="user-section-heading"><strong>已有账号</strong><span>{users.length} 个账号</span></div><div className="user-list">{users.map((item) => <div className="user-list-row" key={item.id}><div className="user-avatar">{initials(item.display_name)}</div><div><strong>{item.display_name}</strong><span>@{item.username}</span></div><Badge>{item.role === 'admin' ? '管理员' : '普通用户'}</Badge><Button variant="ghost" size="sm" onClick={() => { setResetUser(item); setError(''); }}>重置密码</Button></div>)}</div></section>
      <section className="user-management-section"><div className="user-section-heading"><strong>创建账号</strong><span>初始密码至少 12 个字符</span></div><div className="user-create-grid"><label>用户名<Input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="operator" /></label><label>显示名称<Input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="运维同事" /></label><label>初始密码<Input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="至少 12 个字符" /></label><label>角色<NativeSelect value={role} onChange={(event) => setRole(event.target.value as 'admin' | 'user')}><NativeSelectOption value="user">普通用户</NativeSelectOption><NativeSelectOption value="admin">管理员</NativeSelectOption></NativeSelect></label></div></section>
      {resetUser && <div className="reset-box"><strong>重置 @{resetUser.username} 的密码</strong><Input type="password" value={resetPassword} onChange={(event) => setResetPassword(event.target.value)} placeholder="输入新密码（至少 12 个字符）" /><Button variant="outline" onClick={reset} disabled={saving || resetPassword.length < 12}>确认重置</Button></div>}
      {error && <div className="dialog-error"><AlertCircle />{error}</div>}<DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>关闭</Button><Button className="dialog-primary" onClick={create} disabled={saving || !username || !displayName || password.length < 12}>{saving && <LoaderCircle className="spin" />}创建账号</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}

type AuditEvent = { id: number; action: string; entity_type: string; entity_id?: string | null; detail?: string | null; created_at: string; actor_username?: string | null; actor_display_name?: string | null };
function ActivityDialog({ events, onRefresh }: { events: AuditEvent[]; onRefresh: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  return <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (next) void onRefresh(); }}><DialogTrigger render={<button className="nav-item" type="button"><History size={17} /><span>操作记录</span></button>} /><DialogContent className="transfer-dialog activity-dialog"><DialogHeader><div className="dialog-icon"><History size={20} /></div><DialogTitle>操作记录</DialogTitle><DialogDescription>记录登录、账号、节点和传输任务的关键操作。</DialogDescription></DialogHeader><div className="activity-list">{events.length ? events.map((event) => <div className="activity-row" key={event.id}><span>{displayTime(event.created_at)}</span><strong>{event.detail || event.action}</strong><small>{event.actor_display_name || event.actor_username || '系统'} · {event.entity_type}</small></div>) : <div className="empty-state">暂无操作记录</div>}</div><DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>关闭</Button></DialogFooter></DialogContent></Dialog>;
}

function ServiceStatusDialog({ onlineNodes, totalNodes, activeCount, queuedCount, failedCount }: { onlineNodes: number; totalNodes: number; activeCount: number; queuedCount: number; failedCount: number }) {
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const [serviceHealthy, setServiceHealthy] = useState<boolean | null>(null);
  const [checkedAt, setCheckedAt] = useState('');
  async function refreshStatus() {
    setChecking(true);
    try {
      const health = await api<{ ok: boolean }>('/health');
      setServiceHealthy(health.ok);
      setCheckedAt(new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(new Date()));
    } catch {
      setServiceHealthy(false);
      setCheckedAt('');
    } finally {
      setChecking(false);
    }
  }
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger render={<Button variant="ghost" size="icon" className="top-icon-button" aria-label="服务状态" title="服务状态" onClick={() => void refreshStatus()}><TerminalSquare /></Button>} />
    <DialogContent className="transfer-dialog status-dialog">
      <DialogHeader><div className="dialog-icon"><TerminalSquare size={20} /></div><DialogTitle>服务状态</DialogTitle><DialogDescription>查看控制服务、节点和传输队列的实时概况。</DialogDescription></DialogHeader>
      <div className="status-summary-grid">
        <div className={`status-summary-card ${serviceHealthy ? 'status-summary-ok' : serviceHealthy === false ? 'status-summary-error' : ''}`}><span>控制服务</span><strong>{checking ? <><LoaderCircle className="spin" />正在检查</> : serviceHealthy ? <><i />正常运行</> : serviceHealthy === false ? <><AlertCircle />连接异常</> : '等待检查'}</strong><small>{checkedAt ? `北京时间 ${checkedAt} 已检查` : '打开窗口时会连接后台检查'}</small></div>
        <div className="status-summary-card"><span>在线节点</span><strong>{onlineNodes} / {totalNodes}</strong><small>可用于新建传输</small></div>
        <div className="status-summary-card"><span>进行中</span><strong>{activeCount}</strong><small>正在执行的任务</small></div>
        <div className="status-summary-card"><span>等待处理</span><strong>{queuedCount}</strong><small>队列和暂停任务</small></div>
        <div className="status-summary-card"><span>失败任务</span><strong>{failedCount}</strong><small>可在任务列表重试</small></div>
      </div>
      <DialogFooter><Button variant="outline" onClick={() => void refreshStatus()} disabled={checking}>{checking && <LoaderCircle className="spin" />}刷新状态</Button><Button onClick={() => setOpen(false)}>关闭</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}

function HelpCenterDialog() {
  const [open, setOpen] = useState(false);
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger render={<button className="nav-item" type="button"><CircleHelp size={17} /><span>帮助中心</span></button>} />
    <DialogContent className="transfer-dialog help-dialog">
      <DialogHeader><div className="dialog-icon"><CircleHelp size={20} /></div><DialogTitle>帮助中心</DialogTitle><DialogDescription>按下面的流程即可完成节点配置和文件传输。</DialogDescription></DialogHeader>
      <div className="help-list">
        <div><b>1</b><section><strong>添加传输节点</strong><p>管理员在“节点”区域添加服务器，可选择 SSH 私钥或 SSH 密码认证。保存后系统会自动测试连接。</p></section></div>
        <div><b>2</b><section><strong>新建传输任务</strong><p>选择在线的源节点和目标节点，填写源路径与目标目录，再点击“校验并开始直传”。</p></section></div>
        <div><b>3</b><section><strong>查看任务与日志</strong><p>总览页会显示进度、速度和失败原因；管理员可查看全部任务，普通用户只能查看自己的记录。</p></section></div>
        <div><b>4</b><section><strong>账号与权限</strong><p>管理员可以管理账号、节点和传输设置；每个用户都可以在侧边栏修改自己的登录密码。</p></section></div>
      </div>
      <DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>关闭</Button></DialogFooter>
    </DialogContent>
  </Dialog>;
}

function Dashboard({ user, onSignedOut }: { user: User; onSignedOut: () => void }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [nodes, setNodes] = useState<Node[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [transferSettings, setTransferSettings] = useState<TransferSettings | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState<'全部' | '进行中' | '已完成' | '失败'>('全部');
  const [query, setQuery] = useState('');
  const [nodeQuery, setNodeQuery] = useState('');
  const [nodeFilter, setNodeFilter] = useState<'全部' | '在线' | '测试中' | '离线'>('全部');
  const [expandedNodeId, setExpandedNodeId] = useState<string | null>(null);
  const [activeNav, setActiveNav] = useState('总览');
  const [mobileNav, setMobileNav] = useState(false);
  const [nodeToDelete, setNodeToDelete] = useState<Node | null>(null);
  const [deletingNode, setDeletingNode] = useState(false);
  const [taskToDelete, setTaskToDelete] = useState<Task | null>(null);
  const [deletingTask, setDeletingTask] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const selected = tasks.find((task) => task.id === selectedId) ?? tasks[0];

  const loadData = useCallback(async (quiet = false) => {
    try {
      const [taskData, nodeData, settingsData, eventData, userData] = await Promise.all([
        api<{ tasks: Task[] }>('/tasks'),
        api<{ nodes: Node[] }>('/nodes'),
        api<TransferSettings>('/transfer-settings'),
        api<{ events: AuditEvent[] }>('/activity'),
        user.role === 'admin' ? api<{ users: User[] }>('/users') : Promise.resolve({ users: [] }),
      ]);
      setTasks(taskData.tasks); setNodes(nodeData.nodes); setTransferSettings(settingsData); setEvents(eventData.events); setUsers(userData.users); setError('');
      setSelectedId((current) => current && taskData.tasks.some((task) => task.id === current) ? current : taskData.tasks[0]?.id ?? null);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) { onSignedOut(); return; }
      if (!quiet) setError(reason instanceof Error ? reason.message : '加载失败');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [onSignedOut, user.role]);

  useEffect(() => {
    void loadData();
    const timer = window.setInterval(() => void loadData(true), 3000);
    return () => window.clearInterval(timer);
  }, [loadData]);

  const filteredTasks = useMemo(() => tasks.filter((task) => {
    const matchesFilter = filter === '全部' || (filter === '进行中' && ['transferring', 'queued', 'paused'].includes(task.status)) || (filter === '已完成' && task.status === 'completed') || (filter === '失败' && task.status === 'failed');
    return matchesFilter && `${task.name} ${task.source} ${task.destination}`.toLowerCase().includes(query.toLowerCase());
  }), [filter, query, tasks]);

  const filteredNodes = useMemo(() => nodes.filter((node) => {
    const statusLabel = node.status === 'online' ? '在线' : node.status === 'pending' ? '测试中' : '离线';
    const matchesFilter = nodeFilter === '全部' || statusLabel === nodeFilter;
    const searchText = `${node.name} ${node.host} ${node.username} ${node.ssh_port} ${node.auth_type === 'password' ? 'SSH 密码' : 'SSH 私钥'}`.toLowerCase();
    return matchesFilter && searchText.includes(nodeQuery.toLowerCase().trim());
  }), [nodeFilter, nodeQuery, nodes]);

  async function createTask(payload: NewTaskPayload) {
    const data = await api<{ task: Task }>('/tasks', { method: 'POST', body: JSON.stringify(payload) });
    setTasks((current) => [data.task, ...current]); setSelectedId(data.task.id);
  }

  async function createNodes(payload: Record<string, unknown>) {
    await api('/nodes/bulk', { method: 'POST', body: JSON.stringify(payload) });
    await loadData(true);
  }

  async function changePassword(currentPassword: string, newPassword: string) {
    await api('/change-password', { method: 'POST', body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
  }

  async function createUser(payload: { username: string; display_name: string; password: string; role: 'admin' | 'user' }) {
    const data = await api<{ user: User }>('/users', { method: 'POST', body: JSON.stringify(payload) });
    setUsers((current) => [...current, data.user]);
  }

  async function resetUserPassword(id: number, password: string) {
    await api(`/users/${id}/reset-password`, { method: 'POST', body: JSON.stringify({ password }) });
  }

  async function refreshEvents() {
    const data = await api<{ events: AuditEvent[] }>('/activity');
    setEvents(data.events);
  }

  async function saveTransferSettings(settings: TransferSettings) {
    const updated = await api<TransferSettings>('/transfer-settings', { method: 'POST', body: JSON.stringify(settings) });
    setTransferSettings(updated);
  }

  async function testNode(id: string) {
    await api(`/nodes/${id}/test`, { method: 'POST', body: '{}' });
    setNodes((current) => current.map((node) => node.id === id ? { ...node, status: 'pending', last_error: null } : node));
  }

  async function deleteNode(node: Node) {
    setDeletingNode(true);
    try {
      await api(`/nodes/${node.id}`, { method: 'DELETE' });
      setNodes((current) => current.filter((item) => item.id !== node.id));
      setNodeToDelete(null);
      await loadData(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除节点失败');
    } finally {
      setDeletingNode(false);
    }
  }

  async function deleteTask(task: Task) {
    setDeletingTask(true);
    try {
      await api(`/tasks/${task.id}`, { method: 'DELETE' });
      setTasks((current) => current.filter((item) => item.id !== task.id));
      setSelectedId((current) => current === task.id ? null : current);
      setTaskToDelete(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除任务失败');
    } finally {
      setDeletingTask(false);
    }
  }

  async function taskAction(id: string, action: 'pause' | 'resume' | 'retry') {
    const data = await api<{ task: Task }>(`/tasks/${id}/${action}`, { method: 'POST', body: '{}' });
    setTasks((current) => current.map((task) => task.id === id ? data.task : task)); setSelectedId(id);
  }

  async function signOut() {
    try { await api('/logout', { method: 'POST', body: '{}' }); } finally { onSignedOut(); }
  }

  const activeCount = tasks.filter((task) => task.status === 'transferring').length;
  const queuedCount = tasks.filter((task) => ['queued', 'paused'].includes(task.status)).length;
  const completedCount = tasks.filter((task) => task.status === 'completed').length;
  const failedCount = tasks.filter((task) => task.status === 'failed').length;
  const decidedCount = completedCount + failedCount;
  const successRate = decidedCount ? Math.round(completedCount / decidedCount * 100) : 0;
  const onlineNodes = nodes.filter((node) => node.status === 'online').length;
  const navItems = [{ label: '总览', icon: LayoutDashboard }, { label: '传输任务', icon: Activity }, { label: '节点', icon: Server }];
  function jumpToSection(label: string) {
    setActiveNav(label);
    setMobileNav(false);
    const targetId = label === '总览' ? 'overview' : label === '传输任务' ? 'tasks' : 'nodes';
    document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  return (
    <main className="app-shell">
      <aside className={`sidebar ${mobileNav ? 'sidebar-open' : ''}`}>
        <div className="brand-row"><div className="brand-mark"><Zap size={17} fill="currentColor" /></div><span>relay</span><Button className="mobile-close" variant="ghost" size="icon-sm" onClick={() => setMobileNav(false)}><X /></Button></div>
        <div className="workspace-switcher" aria-label={`${user.display_name} 的工作区`}><div className="workspace-avatar">{initials(user.display_name)}</div><div><strong>{user.display_name} 的工作区</strong><span>自托管空间</span></div></div>
        <nav className="main-nav" aria-label="主导航"><p className="nav-label">工作区</p>{navItems.map(({ label, icon: Icon }) => <button key={label} className={`nav-item ${activeNav === label ? 'active' : ''}`} onClick={() => jumpToSection(label)}><Icon size={17} /><span>{label}</span>{label === '传输任务' && queuedCount > 0 && <em>{queuedCount}</em>}</button>)}</nav>
        <div className="sidebar-bottom">{user.role === 'admin' && transferSettings && <TransferSettingsDialog settings={transferSettings} onSave={saveTransferSettings} />}{user.role === 'admin' && <UserManagementDialog users={users} onCreate={createUser} onReset={resetUserPassword} />}<ActivityDialog events={events} onRefresh={refreshEvents} /><ChangePasswordDialog onChange={changePassword} /><HelpCenterDialog /><button className="user-card" onClick={signOut} title="退出登录"><div className="user-avatar">{initials(user.display_name)}</div><div><strong>{user.display_name}</strong><span>@{user.username} · {user.role === 'admin' ? '管理员' : '普通用户'}</span></div><LogOut size={15} /></button></div>
      </aside>
      {mobileNav && <button className="mobile-backdrop" aria-label="关闭导航" onClick={() => setMobileNav(false)} />}

      <section className="main-area">
        <header className="topbar"><div className="breadcrumb"><Button className="mobile-menu" variant="ghost" size="icon-sm" onClick={() => setMobileNav(true)}><Menu /></Button><span>工作区</span><span className="slash">/</span><strong>总览</strong></div><div className="topbar-actions"><div className={`connection ${onlineNodes ? '' : 'waiting'}`}><i />节点在线 <span>{onlineNodes} / {nodes.length}</span></div><ServiceStatusDialog onlineNodes={onlineNodes} totalNodes={nodes.length} activeCount={activeCount} queuedCount={queuedCount} failedCount={failedCount} /><div className="top-avatar" aria-label={user.display_name} title={user.display_name}>{initials(user.display_name)}</div></div></header>
        <div className="content-wrap">
          <div className="page-heading" id="overview"><div><div className="eyebrow"><span className="eyebrow-dot" />传输工作台</div><h1>总览</h1><p>管理节点之间的直接数据传输，所有任务都在这里。</p></div><NewTransferDialog nodes={nodes} onCreate={createTask} /></div>
          {error && <div className="dashboard-alert"><AlertCircle />{error}<button onClick={() => loadData()}>重试</button></div>}
          <section className="stat-grid" aria-label="传输概况"><div className="stat-card"><div className="stat-top"><span>进行中的任务</span><div className="stat-icon mint"><Activity size={17} /></div></div><strong>{activeCount}<small> 个任务</small></strong><div className="stat-foot neutral">队列中 <b>{queuedCount}</b></div></div><div className="stat-card"><div className="stat-top"><span>已完成任务</span><div className="stat-icon violet"><CheckCircle2 size={17} /></div></div><strong>{completedCount}<small> 个任务</small></strong><div className="stat-foot positive">成功率 <b>{successRate}%</b></div></div><div className="stat-card"><div className="stat-top"><span>在线传输节点</span><div className="stat-icon orange"><Gauge size={17} /></div></div><strong>{onlineNodes}<small> / {nodes.length || 0}</small></strong><div className="stat-foot neutral"><span className="line-icon" />至少两台在线可直传</div></div><div className="stat-card chart-card"><div className="stat-top"><span>吞吐量趋势</span><span className="stat-period">近 7 天</span></div><MiniChart hasData={completedCount > 0} /></div></section>

          <section className="active-section"><div className="section-title-row"><div><h2>{selected ? '任务详情' : '开始使用'}</h2><span>{selected ? '所选任务的实时状态' : '添加两台节点后即可直传'}</span></div>{selected && <button className="text-button" onClick={() => { setFilter('全部'); setActiveNav('传输任务'); document.getElementById('tasks')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }}>查看全部 <ArrowUpRight size={14} /></button>}</div>{selected ? <div className="active-card"><div className="active-card-top"><div className="task-identity"><div className="task-icon live"><TaskIcon kind={selected.kind} /></div><div><div className="task-title-line"><h3>{selected.name}</h3><Badge className={statusClass(selected.status)}><span className="badge-dot" />{statusLabel(selected.status, selected.scheduled)}</Badge></div><p><span>{selected.source}</span><ArrowUpRight size={13} /><span>{selected.destination}</span></p></div></div><div className="active-actions">{['queued', 'transferring'].includes(selected.status) && <Button variant="outline" size="sm" onClick={() => taskAction(selected.id, 'pause')}><Pause />暂停</Button>}{selected.status === 'paused' && <Button variant="outline" size="sm" onClick={() => taskAction(selected.id, 'resume')}><Play />继续</Button>}{['failed', 'completed'].includes(selected.status) && <Button variant="outline" size="sm" onClick={() => taskAction(selected.id, 'retry')}><RefreshCw />{selected.status === 'completed' ? '再次传输' : '重试'}</Button>}{(selected.status !== 'transferring' && (selected.status !== 'queued' || selected.scheduled)) && <Button variant="ghost" size="sm" className="delete-task-button" onClick={() => setTaskToDelete(selected)}><Trash2 />删除</Button>}</div></div><div className="task-endpoints"><div><span>源端 · 发送方</span><strong>{selected.source_node?.name || '节点信息不可用'}</strong><small>管理地址：{selected.source_node ? `${selected.source_node.host}:${selected.source_node.ssh_port}` : '—'}</small><small>源路径：{selected.source_path || '—'}</small></div><ArrowRightLeft size={16} /><div><span>目标端 · 接收方</span><strong>{selected.destination_node?.name || '节点信息不可用'}</strong><small>管理地址：{selected.destination_node ? `${selected.destination_node.host}:${selected.destination_node.ssh_port}` : '—'}</small><small>{selected.direct_host ? `本次直传：${selected.direct_host}:${selected.direct_port}（内网）` : `本次直传：${selected.destination_node ? `${selected.destination_node.host}:${selected.destination_node.ssh_port}` : '—'}（公网）`}</small><small>目标路径：{selected.destination_path || '—'}</small></div></div>{selected.error && <div className="task-failure"><AlertCircle /><div><strong>失败原因</strong><p>{selected.error}</p></div></div>}<div className="progress-line"><div className="progress-label"><span>总进度 {selected.transferred} <b>/ {selected.size}</b></span><strong>{selected.progress}%</strong></div><Progress value={selected.progress} className="transfer-progress" /></div><div className="metric-row"><div><span>当前速度</span><strong>{selected.speed}</strong></div><div><span>预计剩余</span><strong>{selected.eta}</strong></div><div><span>大小上限</span><strong>{sizeLimitLabel(selected.max_size_bytes)}</strong></div><div><span>{selected.scheduled ? '预约时间（北京时间）' : '开始时间（北京时间）'}</span><strong>{scheduleLabel(selected)}</strong></div><div className="active-card-status"><span className="pulse-dot" />更新于 {displayTime(selected.updated)}（北京时间）</div></div>{selected.log && <div className="task-log"><div><TerminalSquare />传输日志</div><pre>{selected.log}</pre></div>}</div> : <div className="onboarding-card"><div className="onboarding-icon"><Network /></div><div><h3>节点直传执行器已经就绪</h3><p>添加至少两台在线节点，选择源节点、目标节点和路径后，文件将直接从源节点通过 rsync 传到目标节点。</p></div><div className="onboarding-steps"><span><b>1</b>添加节点</span><span><b>2</b>选择两端路径</span><span><b>3</b>开始直传</span></div></div>}</section>

          <section className="tasks-section" id="tasks"><div className="section-title-row"><div><h2>全部任务</h2><span>共 {tasks.length} 个传输任务{user.role === 'admin' ? ' · 管理员视图包含所有用户' : ''}</span></div></div><div className="table-toolbar"><div className="filter-tabs">{(['全部', '进行中', '已完成', '失败'] as const).map((item) => <button key={item} className={filter === item ? 'selected' : ''} onClick={() => setFilter(item)}>{item}{item === '进行中' && queuedCount > 0 && <span>{queuedCount + activeCount}</span>}</button>)}</div><label className="search-box"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索任务..." /></label></div><div className="task-table-wrap"><table className="task-table"><thead><tr><th>任务名称</th>{user.role === 'admin' && <th>发起人</th>}<th>状态</th><th>总进度</th><th>速度</th><th>更新时间（北京时间）</th><th aria-label="操作" /></tr></thead><tbody>{filteredTasks.map((task) => <tr key={task.id} className={selected?.id === task.id ? 'row-selected' : ''}><td><button type="button" className="table-task-select" aria-label={`查看任务 ${task.name}`} onClick={() => setSelectedId(task.id)}><div className="table-task"><div className={`task-icon small ${statusClass(task.status)}`}><TaskIcon kind={task.kind} /></div><div><strong>{task.name}</strong><span className={task.status === 'failed' && task.error ? 'failed-task-reason' : ''}>{task.status === 'failed' && task.error ? task.error : task.source}</span></div></div></button></td>{user.role === 'admin' && <td><span className="updated-cell">{task.owner_display_name || task.owner_username || '—'}</span></td>}<td><Badge className={statusClass(task.status)}><span className="badge-dot" />{statusLabel(task.status, task.scheduled)}</Badge></td><td><div className="table-progress"><div><span>{task.transferred}</span><span>{task.progress}%</span></div><Progress value={task.progress} className={`tiny-progress ${task.status === 'failed' ? 'failed-progress' : ''}`} /></div></td><td><span className="speed-cell">{task.speed}</span></td><td><span className="updated-cell">{displayTime(task.updated)}</span></td><td><div className="task-row-actions">{task.status === 'failed' && <Button variant="ghost" size="sm" className="retry-button" onClick={(event) => { event.stopPropagation(); void taskAction(task.id, 'retry'); }}><RefreshCw size={14} />重试</Button>}{task.status !== 'transferring' && (task.status !== 'queued' || task.scheduled) && <Button variant="ghost" size="icon-sm" className="delete-task-button" aria-label="删除任务" onClick={(event) => { event.stopPropagation(); setTaskToDelete(task); }}><Trash2 /></Button>}</div></td></tr>)}</tbody></table>{!loading && filteredTasks.length === 0 && <div className="empty-state"><Search size={20} /><span>{tasks.length ? '没有找到匹配的任务' : '还没有任务，点击“新建传输”开始'}</span></div>}{loading && <div className="empty-state"><LoaderCircle className="spin" /><span>正在读取任务...</span></div>}</div></section>

          <section className="nodes-section" id="nodes">
            <div className="section-title-row"><div><h2>传输节点</h2><span>{user.role === 'admin' ? '管理员可添加、测试和删除节点；普通用户可使用已配置节点' : '可使用的传输节点（节点由管理员统一维护）'}</span></div>{user.role === 'admin' && <AddNodesDialog onCreate={createNodes} />}</div>
            <div className="nodes-toolbar">
              <div className="node-filter-tabs" role="tablist" aria-label="节点状态筛选">{(['全部', '在线', '测试中', '离线'] as const).map((item) => <button key={item} type="button" className={nodeFilter === item ? 'selected' : ''} onClick={() => setNodeFilter(item)}>{item}<span>{item === '全部' ? nodes.length : nodes.filter((node) => (node.status === 'online' ? '在线' : node.status === 'pending' ? '测试中' : '离线') === item).length}</span></button>)}</div>
              <label className="node-search"><Search size={16} /><input value={nodeQuery} onChange={(event) => setNodeQuery(event.target.value)} placeholder="搜索节点、地址或用户..." /></label>
            </div>
            {nodes.length ? filteredNodes.length ? <div className="nodes-list">{filteredNodes.map((node) => { const expanded = expandedNodeId === node.id; return <article className={`node-row ${expanded ? 'expanded' : ''}`} key={node.id}>
              <div className="node-row-info"><div className={`node-status-icon ${node.status}`}><Server /></div><div className="node-row-identity"><div><h3>{node.name}</h3><Badge className={`node-badge ${node.status}`}><span className="badge-dot" />{node.status === 'online' ? '在线' : node.status === 'pending' ? '测试中' : '离线'}</Badge></div><p>{node.username}@{node.host}:{node.ssh_port}</p>{expanded && <div className="node-row-details"><span><KeyRound />{node.auth_type === 'password' ? 'SSH 密码' : 'SSH 私钥'}</span><span><Network />管理地址 {node.host}:{node.ssh_port}</span><span title={node.fingerprint || ''}>{node.fingerprint || '密码认证'}</span></div>}{expanded && node.last_error && <div className="node-error"><AlertCircle />{node.last_error}</div>}</div></div>
              <div className="node-row-side"><span className="node-last-seen">{node.last_seen_at ? `最后在线 ${displayTime(node.last_seen_at)}（北京时间）` : '尚未连接成功'}</span><div className="node-row-actions"><Button variant="ghost" size="sm" onClick={() => setExpandedNodeId(expanded ? null : node.id)}>{expanded ? '收起详情' : '详情'}</Button>{user.role === 'admin' && <><Button variant="ghost" size="sm" disabled={node.status === 'pending'} onClick={() => void testNode(node.id)}><RefreshCw />重新测试</Button><Button variant="ghost" size="sm" className="delete-node-button" onClick={() => setNodeToDelete(node)}><Trash2 />删除</Button></>}</div></div>
            </article>; })}</div> : <div className="nodes-empty"><div className="onboarding-icon"><Search /></div><div><h3>没有匹配的节点</h3><p>试试其他名称、地址或状态筛选。</p></div><Button variant="outline" onClick={() => { setNodeQuery(''); setNodeFilter('全部'); }}>清除筛选</Button></div> : <div className="nodes-empty"><div className="onboarding-icon"><ServerCog /></div><div><h3>还没有传输节点</h3><p>{user.role === 'admin' ? '添加至少两台节点后即可开始传输。' : '请联系管理员添加传输节点。'}</p></div>{user.role === 'admin' && <AddNodesDialog onCreate={createNodes} />}</div>}
          </section>
          <footer className="page-footer"><span><span className="footer-status" />控制服务正常运行</span><span>v0.3.0 · 节点直传模式</span></footer>
          <AlertDialog open={Boolean(nodeToDelete)} onOpenChange={(open) => { if (!open && !deletingNode) setNodeToDelete(null); }}>
            <AlertDialogContent>
              <AlertDialogHeader><AlertDialogMedia><AlertCircle /></AlertDialogMedia><AlertDialogTitle>删除节点？</AlertDialogTitle><AlertDialogDescription>将移除“{nodeToDelete?.name}”。已完成的任务记录会保留；若该节点有排队或传输中的任务，系统会拒绝删除。</AlertDialogDescription></AlertDialogHeader>
              <AlertDialogFooter><AlertDialogCancel disabled={deletingNode}>取消</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={deletingNode || !nodeToDelete} onClick={() => { if (nodeToDelete) void deleteNode(nodeToDelete); }}>{deletingNode && <LoaderCircle className="spin" />}{deletingNode ? '正在删除...' : '确认删除'}</AlertDialogAction></AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
          <AlertDialog open={Boolean(taskToDelete)} onOpenChange={(open) => { if (!open && !deletingTask) setTaskToDelete(null); }}><AlertDialogContent><AlertDialogHeader><AlertDialogMedia><Trash2 /></AlertDialogMedia><AlertDialogTitle>删除任务？</AlertDialogTitle><AlertDialogDescription>将删除“{taskToDelete?.name}”的任务记录和日志，不会删除源节点或目标节点上的任何文件。</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel disabled={deletingTask}>取消</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={deletingTask || !taskToDelete} onClick={() => { if (taskToDelete) void deleteTask(taskToDelete); }}>{deletingTask && <LoaderCircle className="spin" />}{deletingTask ? '正在删除...' : '确认删除'}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
        </div>
      </section>
    </main>
  );
}

export default function Home() {
  const [auth, setAuth] = useState<AuthState>({ status: 'loading' });

  const bootstrap = useCallback(async () => {
    setAuth({ status: 'loading' });
    try {
      const data = await api<Bootstrap>('/bootstrap');
      setAuth(data.user ? { status: 'ready', user: data.user } : data.setup_required ? { status: 'setup' } : { status: 'login' });
    } catch (reason) {
      setAuth({ status: 'unavailable', message: reason instanceof Error ? reason.message : '无法连接控制服务' });
    }
  }, []);

  useEffect(() => { void bootstrap(); }, [bootstrap]);

  if (auth.status === 'loading') return <main className="status-screen"><div className="status-card"><div className="status-icon"><LoaderCircle className="spin" /></div><h1>正在连接 relay</h1><p>检查控制服务和登录状态...</p></div></main>;
  if (auth.status === 'unavailable') return <ServiceUnavailable message={auth.message} onRetry={bootstrap} />;
  if (auth.status === 'setup' || auth.status === 'login') return <AuthScreen mode={auth.status} onAuthenticated={(user) => setAuth({ status: 'ready', user })} />;
  return <Dashboard user={auth.user} onSignedOut={() => setAuth({ status: 'login' })} />;
}
