export function healthLabel(health) {
  if (!health.ready) return '当前没有可用机器人';
  return health.status === 'degraded' ? '部分机器人离线' : '运行正常';
}

export async function requestJSON(url, { method = 'GET', body } = {}, acceptedStatuses = []) {
  const response = await fetch(url, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(20000),
  });
  const data = await response.json();
  if (!response.ok && !acceptedStatuses.includes(response.status)) {
    const detail = Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join('; ') : (data.detail ?? '请求失败');
    throw new Error(`${response.status} · ${detail}`);
  }
  return data;
}

if (typeof document !== 'undefined') {
  const cards = new Map();
  const states = { idle: '就绪', running: '运行中', stopped: '已停止', failed: '故障', disabled: '未启用' };
  const robotsContainer = document.querySelector('#robots');
  const connectionError = document.querySelector('#connection-error');
  const capabilities = { brain: 'FAKE BRAIN', room: 'ROOM WS', llm: 'LLM', tts: 'TTS', memory: '长期记忆', viseme: '嘴型' };
  let pollTimer;
  let closed = false;

  function showMessage(card, message, error = false) {
    const element = card.querySelector('.action-message');
    element.textContent = message;
    element.classList.toggle('is-error', error);
  }

  function updateRobot(robot) {
    let card = cards.get(robot.robot_id);
    if (!card) {
      card = document.querySelector('#robot-template').content.firstElementChild.cloneNode(true);
      card.dataset.robotId = robot.robot_id;
      card.querySelector('h3').textContent = robot.robot_id;
      card.querySelector('.robot-avatar').textContent = String(cards.size + 1).padStart(2, '0');
      const input = card.querySelector('input');
      input.id = `input-${robot.robot_id}`;
      input.setAttribute('aria-label', `${robot.robot_id} 文本输入`);
      card.querySelector('label').htmlFor = input.id;
      for (const button of card.querySelectorAll('.robot-actions button')) {
        button.setAttribute('aria-label', `${robot.robot_id} ${button.textContent}`);
        button.addEventListener('click', () => action(card, robot.robot_id, button.dataset.action, button));
      }
      card.querySelector('form').addEventListener('submit', (event) => {
        event.preventDefault();
        const text = input.value.trim();
        if (text) action(card, robot.robot_id, 'turns', card.querySelector('.send-button'), { text });
        else showMessage(card, '请输入非空文本。', true);
      });
      cards.set(robot.robot_id, card);
      robotsContainer.append(card);
    }
    card.dataset.state = robot.state;
    card.querySelector('.robot-room').textContent = `${robot.room_id} / ${robot.persona_id}`;
    card.querySelector('.robot-state').textContent = states[robot.state] ?? robot.state;
    for (const key of ['completed', 'cancelled', 'failures']) card.querySelector(`.${key}`).textContent = robot[key] ?? 0;
    card.querySelector('.queue-depth').textContent = robot.queue_depth ?? 0;
    card.querySelector('.last-input').textContent = robot.last_input || '—';
    card.querySelector('.current-request').textContent = robot.current_request_id || '—';
    card.querySelector('.last-reply').textContent = robot.last_reply || (robot.state === 'running' ? '正在生成 Fake 回复…' : '发送一条消息，开始验证这个机器人。');
    const error = card.querySelector('.runtime-error');
    error.textContent = robot.error || '';
    error.hidden = !robot.error;
    const active = robot.state === 'idle' || robot.state === 'running';
    const send = card.querySelector('.send-button');
    send.disabled = !active || send.dataset.pending === 'true';
    card.querySelector('[data-action="start"]').disabled = active || robot.state === 'disabled';
    card.querySelector('[data-action="stop"]').disabled = !active;
    card.querySelector('[data-action="cancel"]').disabled = robot.state !== 'running';
    const fail = card.querySelector('[data-action="fail"]');
    fail.disabled = !active || fail.dataset.pending === 'true';
  }

  async function action(card, id, name, button, body) {
    if (button.dataset.pending === 'true') return;
    button.dataset.pending = 'true';
    button.disabled = true;
    showMessage(card, name === 'turns' ? '回合已提交；其它机器人头可同时处理自己的回合。' : '正在执行机器人头操作…');
    try {
      const result = await requestJSON(`/api/robots/${encodeURIComponent(id)}/${name}`, { method: 'POST', body });
      if (result.state) updateRobot(result);
      const labels = { completed: '回合完成', cancelled: '回合已取消', failed: '模拟故障已触发', discarded: '排队回合已丢弃' };
      const label = labels[result.status] ?? { cancel: '已请求取消当前回合', stop: '该机器人已停止', start: '该机器人已启动' }[name];
      showMessage(card, `${label ?? '操作完成'}${result.reply ? ` · ${result.reply}` : ''}`, result.status === 'failed');
    } catch (error) {
      showMessage(card, error.message, true);
    } finally {
      delete button.dataset.pending;
      button.disabled = false;
      await refresh();
    }
  }

  let refreshing;
  function refresh() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        const health = await requestJSON('/health', {}, [503]);
        document.querySelector('#health-status').textContent = healthLabel(health);
        document.querySelector('#health-dot').dataset.status = health.ready ? health.status : 'unavailable';
        document.querySelector('#process-id').textContent = health.pid ?? '—';
        document.querySelector('#robot-count').textContent = `${health.robots.length} 个机器人头`;
        for (const robot of health.robots) updateRobot(robot);
        const capabilityList = document.querySelector('#capability-list');
        capabilityList.replaceChildren();
        for (const [key, label] of Object.entries(capabilities)) {
          const item = document.createElement('span');
          item.className = `capability ${key === 'brain' ? 'local' : ''}`;
          const state = health.capabilities?.[key];
          item.textContent = `${label} · ${state === 'fake' ? '本地 Fake' : state === 'not_started' ? '未启动' : '未连接'}`;
          capabilityList.append(item);
        }
        connectionError.hidden = true;
      } catch (error) {
        document.querySelector('#health-status').textContent = 'Runtime 连接中断';
        document.querySelector('#health-dot').dataset.status = 'unavailable';
        connectionError.textContent = `健康检查失败 · ${error.message}`;
        connectionError.hidden = false;
      } finally {
        refreshing = undefined;
      }
    })();
    return refreshing;
  }

  async function poll() {
    await refresh();
    if (!closed) pollTimer = setTimeout(poll, 750);
  }
  window.addEventListener('pagehide', () => { closed = true; clearTimeout(pollTimer); });
  window.addEventListener('pageshow', (event) => {
    if (!event.persisted || !closed) return;
    closed = false;
    void poll();
  });
  void poll();
}
