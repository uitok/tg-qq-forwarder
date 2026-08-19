import { useEffect, useMemo, useState } from "react"
import { Activity, ArrowRight, Check, CircleUserRound, LogOut, MessageCircle, Plus, RefreshCw, Send, Shield, Trash2, Users, X } from "lucide-react"
import { Badge, Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Select, Table } from "./components/ui"

type User = { id: number; username: string; role: "admin" | "user"; active: boolean; created_at: string }
type Dialog = { id: number; title: string; kind: "private" | "group"; username?: string | null }
type Target = { id: number; title: string }
type Route = { id: number; owner_user_id: number; tg_chat_id: number; tg_title: string; qq_type: "private" | "group"; qq_id: number; enabled: boolean; last_error?: string | null; created_at: string }
type Status = { telegram_ready: boolean; telegram_error?: string | null; routes: number; enabled_routes: number }

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json", ...(init?.headers || {}) }, ...init })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || "请求失败")
  return body as T
}

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError(""); setBusy(true)
    try { const result = await api<{ user: User }>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }); onLogin(result.user) }
    catch (err) { setError(err instanceof Error ? err.message : "登录失败") }
    finally { setBusy(false) }
  }
  return <main className="login-shell"><div className="login-brand"><div className="brand-mark"><Send size={20} /></div><span>TG → QQ</span></div><Card className="login-card"><CardHeader><CardTitle>登录转发中心</CardTitle><CardDescription>使用管理员或成员账号继续</CardDescription></CardHeader><CardContent><form className="stack-form" onSubmit={submit}><label>用户名<Input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required /></label><label>密码<Input autoComplete="current-password" type="password" value={password} onChange={e => setPassword(e.target.value)} required /></label>{error && <div className="form-error"><X size={16} />{error}</div>}<Button type="submit" disabled={busy}>{busy ? "登录中..." : "登录"}<ArrowRight size={16} /></Button></form></CardContent></Card><p className="login-foot">Telegram → NapCat OneBot</p></main>
}

function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined)
  useEffect(() => { api<{ user: User }>("/api/auth/me").then(result => setUser(result.user)).catch(() => setUser(null)) }, [])
  if (user === undefined) return <div className="loading-screen"><RefreshCw className="spin" size={20} />加载中</div>
  if (!user) return <Login onLogin={setUser} />
  return <Dashboard user={user} onLogout={() => { api("/api/auth/logout", { method: "POST" }).finally(() => setUser(null)) }} />
}

function Dashboard({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [tab, setTab] = useState("routes")
  const [routes, setRoutes] = useState<Route[]>([])
  const [status, setStatus] = useState<Status | null>(null)
  const [toast, setToast] = useState("")
  async function refresh() {
    try { const [routeData, statusData] = await Promise.all([api<Route[]>("/api/routes"), api<Status>("/api/status")]); setRoutes(routeData); setStatus(statusData) }
    catch (err) { setToast(err instanceof Error ? err.message : "刷新失败") }
  }
  useEffect(() => { refresh() }, [])
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><div className="brand-mark"><Send size={18} /></div><div><strong>TG → QQ</strong><small>转发中心</small></div></div><nav><button className={tab === "routes" ? "nav-item nav-item-active" : "nav-item"} onClick={() => setTab("routes")}><Activity size={17} />转发规则</button>{user.role === "admin" && <button className={tab === "users" ? "nav-item nav-item-active" : "nav-item"} onClick={() => setTab("users")}><Shield size={17} />用户后台</button>}</nav><div className="sidebar-bottom"><div className="profile"><CircleUserRound size={20} /><div><strong>{user.username}</strong><small>{user.role === "admin" ? "管理员" : "成员"}</small></div></div><button className="nav-item" onClick={onLogout}><LogOut size={17} />退出登录</button></div></aside><main className="main-content"><header className="topbar"><div><p className="eyebrow">控制台</p><h1>{tab === "routes" ? "转发规则" : "用户后台"}</h1></div><div className="topbar-actions"><Badge tone={status?.telegram_ready ? "success" : "warning"}>{status?.telegram_ready ? "Telegram 已连接" : "Telegram 未连接"}</Badge><Button variant="ghost" onClick={refresh} title="刷新数据"><RefreshCw size={17} /></Button></div></header>{toast && <div className="toast">{toast}<button onClick={() => setToast("")}><X size={15} /></button></div>}{tab === "routes" ? <RoutesView routes={routes} setRoutes={setRoutes} status={status} notify={setToast} refresh={refresh} /> : <UsersView notify={setToast} />}</main></div>
}

function RoutesView({ routes, setRoutes, status, notify, refresh }: { routes: Route[]; setRoutes: React.Dispatch<React.SetStateAction<Route[]>>; status: Status | null; notify: (message: string) => void; refresh: () => Promise<void> }) {
  const [showForm, setShowForm] = useState(false)
  async function toggle(route: Route) { try { const updated = await api<Route>(`/api/routes/${route.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !route.enabled }) }); setRoutes(items => items.map(item => item.id === route.id ? updated : item)) } catch (err) { notify(err instanceof Error ? err.message : "更新失败") } }
  async function remove(route: Route) { if (!window.confirm(`删除“${route.tg_title}”的转发规则？`)) return; try { await api(`/api/routes/${route.id}`, { method: "DELETE" }); setRoutes(items => items.filter(item => item.id !== route.id)) } catch (err) { notify(err instanceof Error ? err.message : "删除失败") } }
  return <div className="page-body"><div className="metric-grid"><Card><CardContent className="metric"><span className="metric-icon metric-icon-blue"><Activity size={18} /></span><div><strong>{status?.routes ?? 0}</strong><small>全部规则</small></div></CardContent></Card><Card><CardContent className="metric"><span className="metric-icon metric-icon-green"><Check size={18} /></span><div><strong>{status?.enabled_routes ?? 0}</strong><small>正在转发</small></div></CardContent></Card><Card><CardContent className="metric"><span className="metric-icon metric-icon-amber"><MessageCircle size={18} /></span><div><strong>{status?.telegram_ready ? "正常" : "待连接"}</strong><small>Telegram 状态</small></div></CardContent></Card></div>{status?.telegram_error && <div className="status-note"><div className="status-note-copy"><span><X size={16} />{status.telegram_error}</span><small>当前账号可以在这里完成 Telegram 首次授权。</small></div><TelegramAuth onSuccess={refresh} onError={notify} /></div>}<Card><CardHeader className="section-header"><div><CardTitle>路由列表</CardTitle><CardDescription>将 Telegram 私聊或群组消息发送到指定 QQ 目标</CardDescription></div><Button onClick={() => setShowForm(value => !value)}>{showForm ? <X size={16} /> : <Plus size={16} />}{showForm ? "关闭" : "新增规则"}</Button></CardHeader>{showForm && <RouteForm onCreated={route => { setRoutes(items => [route, ...items]); setShowForm(false); notify("规则已创建") }} onError={notify} />}{routes.length === 0 ? <div className="empty-state"><MessageCircle size={28} /><strong>还没有转发规则</strong><span>新增一条规则，选择 Telegram 来源和 QQ 私聊或群组目标。</span></div> : <Table><thead><tr><th>Telegram 来源</th><th>QQ 目标</th><th>状态</th><th>创建者</th><th></th></tr></thead><tbody>{routes.map(route => <tr key={route.id}><td><div className="route-name"><span className={`type-icon ${route.tg_title ? "type-icon-blue" : ""}`}>{route.qq_type === "group" ? <Users size={15} /> : <MessageCircle size={15} />}</span><div><strong>{route.tg_title}</strong><small>ID {route.tg_chat_id}</small></div></div></td><td><div><strong>{route.qq_type === "group" ? "QQ群" : "QQ好友"}</strong><small>ID {route.qq_id}</small></div></td><td><button className="status-toggle" onClick={() => toggle(route)}><Badge tone={route.enabled ? "success" : "neutral"}>{route.enabled ? "运行中" : "已停用"}</Badge></button>{route.last_error && <small className="error-text" title={route.last_error}>最近失败</small>}</td><td><small>用户 #{route.owner_user_id}</small></td><td className="row-actions"><Button variant="ghost" title="删除规则" onClick={() => remove(route)}><Trash2 size={16} /></Button></td></tr>)}</tbody></Table>}</Card></div>
}

function TelegramAuth({ onSuccess, onError }: { onSuccess: () => Promise<void>; onError: (message: string) => void }) {
  const [phone, setPhone] = useState("")
  const [code, setCode] = useState("")
  const [password, setPassword] = useState("")
  const [sent, setSent] = useState(false)
  const [passwordRequired, setPasswordRequired] = useState(false)
  const [busy, setBusy] = useState(false)
  async function sendCode() { setBusy(true); try { await api("/api/telegram/auth/send-code", { method: "POST", body: JSON.stringify({ phone }) }); setSent(true); onError("验证码已发送到 Telegram") } catch (err) { onError(err instanceof Error ? err.message : "验证码发送失败") } finally { setBusy(false) } }
  async function verify() { setBusy(true); try { const result = await api<{ ok: boolean; password_required?: boolean; account?: string }>("/api/telegram/auth/verify", { method: "POST", body: JSON.stringify({ code: code || undefined, password: password || undefined }) }); if (result.password_required) { setPasswordRequired(true); onError("请输入 Telegram 二步验证密码") } else { onError(`Telegram 已连接${result.account ? `：${result.account}` : ""}`); await onSuccess() } } catch (err) { onError(err instanceof Error ? err.message : "Telegram 授权失败") } finally { setBusy(false) } }
  return <div className="telegram-auth"><div className="telegram-auth-fields">{!sent && <Input placeholder="手机号，例如 +8613800000000" value={phone} onChange={event => setPhone(event.target.value)} />}{sent && !passwordRequired && <Input placeholder="Telegram 验证码" value={code} onChange={event => setCode(event.target.value)} autoComplete="one-time-code" />}{passwordRequired && <Input placeholder="Telegram 二步验证密码" type="password" value={password} onChange={event => setPassword(event.target.value)} />}</div><Button variant="secondary" onClick={sent ? verify : sendCode} disabled={busy || (!sent && !phone) || (sent && !passwordRequired && !code) || (passwordRequired && !password)}>{busy ? "处理中..." : sent ? "确认授权" : "发送验证码"}<ArrowRight size={15} /></Button></div>
}

function RouteForm({ onCreated, onError }: { onCreated: (route: Route) => void; onError: (message: string) => void }) {
  const [dialogs, setDialogs] = useState<Dialog[]>([])
  const [targets, setTargets] = useState<Target[]>([])
  const [tgKind, setTgKind] = useState<"private" | "group">("private")
  const [qqType, setQqType] = useState<"private" | "group">("private")
  const [tgId, setTgId] = useState("")
  const [tgUsername, setTgUsername] = useState("")
  const [qqId, setQqId] = useState("")
  const [targetError, setTargetError] = useState("")
  const [busy, setBusy] = useState(false)
  useEffect(() => { api<Dialog[]>("/api/telegram/dialogs").then(setDialogs).catch(err => onError(err instanceof Error ? err.message : "无法读取 Telegram 对话")) }, [])
  async function loadTargets() { setQqId(""); setTargetError(""); try { setTargets(await api<Target[]>(`/api/qq/targets?kind=${qqType}`)) } catch (err) { setTargets([]); const message = err instanceof Error ? err.message : "无法读取 QQ 目标"; setTargetError(message) } }
  useEffect(() => { loadTargets() }, [qqType])
  const visibleDialogs = useMemo(() => dialogs.filter(item => item.kind === tgKind), [dialogs, tgKind])
  const selectedDialog = useMemo(() => dialogs.find(item => String(item.id) === tgId), [dialogs, tgId])
  async function submit(event: React.FormEvent) { event.preventDefault(); const selectedTarget = targets.find(item => String(item.id) === qqId); const targetId = selectedTarget?.id ?? Number(qqId); const hasTelegramSource = Boolean(selectedDialog || tgUsername.trim()); if (!hasTelegramSource || !Number.isSafeInteger(targetId) || targetId <= 0) return onError("请选择 Telegram 对话或填写用户名，并填写有效的 QQ 目标 ID"); setBusy(true); try { const route = await api<Route>("/api/routes", { method: "POST", body: JSON.stringify({ tg_chat_id: selectedDialog?.id, tg_title: selectedDialog?.title || "", tg_username: tgUsername.trim() || undefined, qq_type: qqType, qq_id: targetId }) }); onCreated(route) } catch (err) { onError(err instanceof Error ? err.message : "创建失败") } finally { setBusy(false) } }
  return <form className="route-form" onSubmit={submit}><div className="route-form-grid"><div><span className="field-label">Telegram 来源类型</span><div className="segmented"><button type="button" className={tgKind === "private" ? "segment-active" : ""} onClick={() => { setTgKind("private"); setTgId("") }}><MessageCircle size={16} />私聊</button><button type="button" className={tgKind === "group" ? "segment-active" : ""} onClick={() => { setTgKind("group"); setTgId("") }}><Users size={16} />群组</button></div></div><label><span>Telegram 对话</span><Select value={tgId} onChange={e => { setTgId(e.target.value); setTgUsername("") }}><option value="">选择 Telegram {tgKind === "group" ? "群组" : "私聊"}</option>{visibleDialogs.map(dialog => <option key={dialog.id} value={dialog.id}>{dialog.title}{dialog.username ? ` · @${dialog.username}` : ""}</option>)}</Select></label><label><span>或填写 Telegram 用户名</span><Input placeholder={tgKind === "group" ? "@群组用户名" : "@用户用户名"} value={tgUsername} onChange={e => { setTgUsername(e.target.value); setTgId("") }} /></label><div><span className="field-label">QQ 目标类型</span><div className="segmented"><button type="button" className={qqType === "private" ? "segment-active" : ""} onClick={() => setQqType("private")}><MessageCircle size={16} />私聊</button><button type="button" className={qqType === "group" ? "segment-active" : ""} onClick={() => setQqType("group")}><Users size={16} />群组</button></div></div><label><span>QQ {qqType === "group" ? "群组" : "好友"}</span><Select value={targets.some(target => String(target.id) === qqId) ? qqId : ""} onChange={e => setQqId(e.target.value)}><option value="">{targets.length ? `选择 QQ ${qqType === "group" ? "群组" : "好友"}` : "NapCat 列表不可用"}</option>{targets.map(target => <option key={target.id} value={target.id}>{target.title} · {target.id}</option>)}</Select></label><label><span>或手动填写 QQ ID</span><Input inputMode="numeric" placeholder={qqType === "group" ? "QQ群号" : "QQ 号"} value={qqId} onChange={e => setQqId(e.target.value.replace(/\D/g, ""))} required /></label></div>{targetError && <div className="form-error"><X size={16} />{targetError}<Button type="button" variant="ghost" onClick={loadTargets}><RefreshCw size={15} />重试</Button></div>}<div className="form-actions"><span className="form-hint">Telegram 支持列表选择或输入 @用户名；QQ 列表需要 NapCat OneBot HTTP 正常响应。</span><Button type="submit" disabled={busy || (!visibleDialogs.length && !tgUsername.trim()) || !qqId}>{busy ? "保存中..." : "保存规则"}<Check size={16} /></Button></div></form>
}

function UsersView({ notify }: { notify: (message: string) => void }) {
  const [users, setUsers] = useState<User[]>([])
  const [form, setForm] = useState({ username: "", password: "", role: "user" as "admin" | "user" })
  const [busy, setBusy] = useState(false)
  async function refresh() { try { setUsers(await api<User[]>("/api/admin/users")) } catch (err) { notify(err instanceof Error ? err.message : "无法读取用户") } }
  useEffect(() => { refresh() }, [])
  async function create(event: React.FormEvent) { event.preventDefault(); setBusy(true); try { const user = await api<User>("/api/admin/users", { method: "POST", body: JSON.stringify(form) }); setUsers(items => [...items, user]); setForm({ username: "", password: "", role: "user" }); notify("用户已创建") } catch (err) { notify(err instanceof Error ? err.message : "创建失败") } finally { setBusy(false) } }
  async function toggle(user: User) { try { const updated = await api<User>(`/api/admin/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ active: !user.active }) }); setUsers(items => items.map(item => item.id === user.id ? updated : item)) } catch (err) { notify(err instanceof Error ? err.message : "更新失败") } }
  return <div className="page-body"><Card><CardHeader><CardTitle>创建成员</CardTitle><CardDescription>为面板添加可独立登录的用户，成员只能管理自己的规则。</CardDescription></CardHeader><CardContent><form className="user-form" onSubmit={create}><Input placeholder="用户名" value={form.username} onChange={e => setForm({ ...form, username: e.target.value })} required /><Input placeholder="至少 8 位密码" type="password" value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} minLength={8} required /><Select value={form.role} onChange={e => setForm({ ...form, role: e.target.value as "admin" | "user" })}><option value="user">成员</option><option value="admin">管理员</option></Select><Button disabled={busy}><Plus size={16} />创建用户</Button></form></CardContent></Card><Card><CardHeader><CardTitle>账号列表</CardTitle><CardDescription>停用账号后，该账号的会话会在下一次请求时失效。</CardDescription></CardHeader><Table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>创建时间</th><th></th></tr></thead><tbody>{users.map(item => <tr key={item.id}><td><div className="route-name"><span className="avatar"><CircleUserRound size={16} /></span><strong>{item.username}</strong></div></td><td><Badge>{item.role === "admin" ? "管理员" : "成员"}</Badge></td><td><button className="status-toggle" onClick={() => toggle(item)}><Badge tone={item.active ? "success" : "neutral"}>{item.active ? "启用" : "停用"}</Badge></button></td><td><small>{new Date(item.created_at + "Z").toLocaleString()}</small></td><td></td></tr>)}</tbody></Table></Card></div>
}

export default App
