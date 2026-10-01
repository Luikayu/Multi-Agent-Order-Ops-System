"""Dependency-free operations dashboard; all mutations call public APIs."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Order Agent Ops</title>
  <style>
    :root { color-scheme: light; font-family: Inter, system-ui, sans-serif; }
    body { margin: 0; background: #f3f5f7; color: #17202a; }
    header { padding: 24px 32px; color: white; background: #17324d; }
    header h1 { margin: 0 0 6px; font-size: 24px; }
    header p { margin: 0; color: #d9e6f2; }
    main { max-width: 1280px; margin: 0 auto; padding: 24px; }
    .toolbar, .grid { display: grid; gap: 16px; }
    .toolbar { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom: 16px; }
    .grid { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
    section { background: white; border: 1px solid #dce3e9; border-radius: 10px; padding: 16px; box-shadow: 0 2px 8px #17324d12; }
    h2 { margin: 0 0 12px; font-size: 17px; }
    button, input, select { font: inherit; padding: 8px 10px; border-radius: 6px; border: 1px solid #aebbc6; }
    button { cursor: pointer; color: white; background: #176b87; border-color: #176b87; }
    button.secondary { color: #17324d; background: white; }
    button.danger { background: #9c2f2f; border-color: #9c2f2f; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    pre { min-height: 60px; max-height: 320px; overflow: auto; padding: 12px; background: #f7f9fb; border-radius: 6px; white-space: pre-wrap; word-break: break-word; }
    .approval { padding: 10px 0; border-top: 1px solid #edf0f2; }
    .approval:first-child { border-top: 0; }
    .status { min-height: 24px; margin: 8px 0 16px; color: #41586b; }
    code { background: #edf2f5; padding: 2px 5px; border-radius: 4px; }
  </style>
</head>
<body>
  <header>
    <h1>订单多智能体运维台</h1>
    <p>页面只读取状态或调用公开 API；刷新页面不会修改业务状态。</p>
  </header>
  <main>
    <div class="toolbar">
      <section>
        <h2>刷新与检测</h2>
        <div class="row">
          <button id="refresh">刷新只读数据</button>
          <button id="detect">运行异常检测</button>
        </div>
      </section>
      <section>
        <h2>故障场景</h2>
        <div class="row">
          <select id="fault-select"></select>
          <button id="fault-on" class="danger">启用</button>
          <button id="fault-off" class="secondary">停用</button>
        </div>
      </section>
      <section>
        <h2>Trace 查询</h2>
        <div class="row">
          <input id="trace-id" placeholder="TRACE-...">
          <button id="trace-query">查询</button>
        </div>
      </section>
    </div>
    <div id="status" class="status">正在加载……</div>
    <div class="grid">
      <section><h2>系统健康</h2><pre id="health"></pre></section>
      <section><h2>订单</h2><pre id="orders"></pre></section>
      <section><h2>事件与诊断</h2><pre id="incidents"></pre></section>
      <section><h2>证据</h2><pre id="evidence"></pre></section>
      <section><h2>审批（待审批项可确认或拒绝）</h2><div id="approvals"></div></section>
      <section><h2>处置动作</h2><pre id="actions"></pre></section>
      <section><h2>操作结果 / Trace</h2><pre id="result"></pre></section>
    </div>
  </main>
  <script>
    const pretty = value => JSON.stringify(value, null, 2);
    const setStatus = message => document.getElementById('status').textContent = message;
    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {'Content-Type': 'application/json', ...(options.headers || {})}
      });
      const data = await response.json();
      if (!response.ok) throw new Error(`${data.error_code || response.status}: ${data.message || pretty(data)}`);
      return data;
    }
    function show(id, value) { document.getElementById(id).textContent = pretty(value); }
    function renderApprovals(items) {
      const root = document.getElementById('approvals');
      root.replaceChildren();
      if (!items.length) { root.textContent = '暂无审批'; return; }
      for (const item of items) {
        const box = document.createElement('div');
        box.className = 'approval';
        const text = document.createElement('pre');
        text.textContent = pretty(item);
        box.appendChild(text);
        if (item.status === 'pending') {
          const controls = document.createElement('div');
          controls.className = 'row';
          for (const [label, approved, className] of [['确认', true, ''], ['拒绝', false, 'danger']]) {
            const button = document.createElement('button');
            button.textContent = label;
            button.className = className;
            button.onclick = () => decideApproval(item.approval_id, approved);
            controls.appendChild(button);
          }
          box.appendChild(controls);
        }
        root.appendChild(box);
      }
    }
    async function loadDashboard() {
      setStatus('正在刷新只读数据……');
      try {
        const [health, orders, incidents, evidence, approvals, actions, faults] = await Promise.all([
          api('/ops/health'), api('/ops/orders'), api('/ops/incidents'), api('/ops/evidence'),
          api('/ops/approvals'), api('/ops/actions'), api('/ops/faults')
        ]);
        show('health', health); show('orders', orders); show('incidents', incidents);
        show('evidence', evidence); show('actions', actions); renderApprovals(approvals);
        const select = document.getElementById('fault-select');
        const previous = select.value;
        select.replaceChildren();
        for (const fault of faults) {
          const option = document.createElement('option');
          option.value = fault.name;
          option.textContent = `${fault.name}${fault.active ? '（已启用）' : ''}`;
          select.appendChild(option);
        }
        if ([...select.options].some(option => option.value === previous)) select.value = previous;
        setStatus(`刷新完成：${new Date().toLocaleTimeString()}`);
      } catch (error) { setStatus(error.message); }
    }
    async function decideApproval(id, approved) {
      try {
        const data = await api(`/ops/approvals/${encodeURIComponent(id)}/decision`, {
          method: 'POST', body: JSON.stringify({approved, decided_by: 'dashboard-operator'})
        });
        show('result', data);
        setStatus(approved ? '审批已确认；令牌只在本次结果中显示。' : '审批已拒绝。');
        await loadDashboard();
      } catch (error) { setStatus(error.message); }
    }
    async function updateFault(action) {
      const name = document.getElementById('fault-select').value;
      try {
        show('result', await api(`/ops/faults/${encodeURIComponent(name)}/${action}`, {method: 'POST'}));
        await loadDashboard();
      } catch (error) { setStatus(error.message); }
    }
    document.getElementById('refresh').onclick = loadDashboard;
    document.getElementById('detect').onclick = async () => {
      try { show('result', await api('/ops/detect', {method: 'POST'})); await loadDashboard(); }
      catch (error) { setStatus(error.message); }
    };
    document.getElementById('fault-on').onclick = () => updateFault('activate');
    document.getElementById('fault-off').onclick = () => updateFault('deactivate');
    document.getElementById('trace-query').onclick = async () => {
      const id = document.getElementById('trace-id').value.trim();
      try { show('result', await api(`/ops/traces/${encodeURIComponent(id)}`)); }
      catch (error) { setStatus(error.message); }
    };
    loadDashboard();
  </script>
</body>
</html>"""


def create_dashboard_router() -> APIRouter:
    router = APIRouter(tags=["operations-ui"])

    @router.get("/", include_in_schema=False)
    def dashboard_redirect() -> RedirectResponse:
        return RedirectResponse(url="/ops/ui")

    @router.get("/ops/ui", response_class=HTMLResponse, include_in_schema=False)
    def operations_dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    return router
