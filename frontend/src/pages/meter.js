import {api} from '../api/client.js';
import {errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {meterStates, pairStatus, versionStates} from '../constants/enums.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

function windowForm(datasets) {
  if (state.account.role !== 'analyst') return '';
  const options = datasets.map(row => `<option value="${row.id}">${esc(row.name)} · #${row.id}</option>`).join('');
  return `<form id="window-sync" class="inline-form">
    <label>数据版本<select name="dataset_id">${options}</select></label>
    <label style="grid-column:1/-1">洗程窗口（每行一个 JSON：shop、batch_id、started_at、finished_at，时刻须含时区）
      <textarea name="windows" required placeholder='{"shop":"东港工坊","batch_id":"WX-16-11","started_at":"2026-11-16T08:00:00+08:00","finished_at":"2026-11-16T10:00:00+08:00"}'></textarea>
    </label>
    <button type="submit">登记洗程窗口</button>${errorSlot}
  </form>`;
}

function readingForm() {
  if (state.account.role !== 'analyst') return '';
  return `<form id="meter-upload" class="inline-form">
    <label>读数编号<input name="reading_id" required maxlength="60" placeholder="如 M-WX-19-22"></label>
    <label>工坊<input name="shop" required maxlength="80" placeholder="工坊全名"></label>
    <label>抄表时刻（含时区）<input name="read_at" type="datetime-local" step="1" required></label>
    <label>读数方量（L）<input name="liters" type="number" step="any" min="0" max="100000" required></label>
    <button type="submit">登记读数并重算</button>${errorSlot}
    <p class="hint">读数只追加、不可覆盖；迟到读数请用新的读数编号，系统会连同该工坊旧配对整体重算。</p>
  </form>`;
}

function pairingTable(version) {
  if (!version) return '<p class="empty">该工坊还没有生效的对账版本。</p>';
  const rows = version.pairings.map(pair => ({
    读数编号: pair.reading_id,
    洗涤批号: pair.batch_id,
    抄表时刻: pair.read_at.replace('T', ' ').slice(0, 19),
    读数方量: pair.reading_liters,
    批次实耗: pair.batch_liters,
    水表差: pair.gap,
    状态: pairStatus[pair.status] ?? pair.status,
  }));
  const issues = [
    ...version.unmatched_reading_ids.map(id => ({类型: '未配读数', 对象: id, 说明: '未落在任何洗程窗口'})),
    ...version.unmatched_batch_ids.map(id => ({类型: '未配批次', 对象: id, 说明: '没有可配水表读数，不通过对账'})),
    ...Object.entries(version.disputed_edges).map(([reading, batches]) =>
      ({类型: '争议配对', 对象: reading, 说明: `可互换批次：${batches.join('、')}`})),
  ];
  return `<h4>工坊「${esc(version.shop)}」版本 v${version.version} · ${versionStates[version.state]}</h4>
    <p class="hint">最大配对 ${version.max_cardinality} 对 · 确定 ${version.certain_count} · 争议 ${version.disputed_count} · 确定配对水表差合计 ${version.meter_gap_total} L</p>
    ${table(rows)}
    ${issues.length ? `<h4>未通过对账项</h4>${table(issues)}` : '<p class="notice">本版本全部配对确定，无未配或争议项。</p>'}`;
}

export async function meter() {
  const [versionBox, readingBox, jobBox, datasets] = await Promise.all([
    api('/meter-versions'), api('/meter-readings'), api('/meter-jobs'), api('/datasets'),
  ]);
  const activeRows = versionBox.active.map(version => ({
    工坊: version.shop,
    版本: `v${version.version}`,
    状态: versionStates[version.state] ?? version.state,
    最大配对: version.max_cardinality,
    确定: version.certain_count,
    争议: version.disputed_count,
    未配读数: version.unmatched_reading_ids.length,
    未配批次: version.unmatched_batch_ids.length,
    生成时间: time(version.created),
  }));
  const historyRows = versionBox.history.map(version => ({
    工坊: version.shop,
    版本: `v${version.version}`,
    状态: versionStates[version.state] ?? version.state,
    确定: version.certain_count,
    争议: version.disputed_count,
    取代者: version.superseded_by ? `v${version.superseded_by}` : '—',
    错误: version.error ?? '—',
    生成时间: time(version.created),
  }));
  const readingRows = readingBox.readings.map(row => ({
    读数编号: row.reading_id,
    工坊: row.shop,
    抄表时刻: new Date(row.read_at * 1000).toLocaleString('zh-CN'),
    收到时间: time(row.received_at),
    方量: row.liters,
    录入: row.owner,
  }));
  const jobRows = jobBox.jobs.map(job => ({
    任务: job.id,
    工坊: job.shop,
    状态: meterStates[job.state] ?? job.state,
    对账版本: job.version_id ? `#${job.version_id}` : '—',
    错误: job.error ?? '—',
    创建: time(job.created),
    完成: time(job.finished),
  }));
  const detail = versionBox.active.map(pairingTable).join('');
  const busy = jobBox.jobs.some(job => job.state === 'queued' || job.state === 'running');
  return `<section>
    <h2>洗程窗口登记</h2>
    <p class="hint">读数时刻须落在同工坊批次的洗程窗口内才构成候选。先为数据版本登记每个批次的起止时刻。</p>
    ${windowForm(datasets)}
  </section>
  <section>
    <h2>抄表读数登记</h2>
    <p class="hint">抄表组单独交读数，记录不含洗涤批号。系统按同工坊且读数时刻落在洗程窗口内建立候选，求最大基数匹配；仅每种最大配法都相同的配对才记为确定，争议与未配项不通过水表对账。</p>
    ${readingForm()}
  </section>
  <section><h3>生效对账版本</h3>${table(activeRows)}</section>
  <section><h3>配对与争议明细</h3>${detail || '<p class="empty">暂无对账版本。</p>'}</section>
  <section><h3>重算任务（失败保留旧版，旧结果不覆盖新结果）</h3>${busy ? '<p data-poll class="hint">工坊重算进行中，页面自动刷新。</p>' : ''}${table(jobRows)}</section>
  <section><h3>已登记读数（追加式）</h3>${table(readingRows)}</section>
  <section><h3>版本历史（不可变）</h3>${table(historyRows)}</section>`;
}

export function bindMeter(root, refresh) {
  bindForm(root, '#meter-upload', async form => {
    const data = Object.fromEntries(new FormData(form).entries());
    data.liters = Number(data.liters);
    if (data.read_at) data.read_at = new Date(data.read_at).toISOString();
    const result = await api('/meter-readings', {readings: [data]});
    state.status = `已登记 ${result.accepted} 条读数，工坊 ${result.shops.join('、')} 已排队重算。`;
  }, refresh);
  bindForm(root, '#window-sync', async form => {
    const datasetId = Number(form.elements.dataset_id.value);
    const lines = form.elements.windows.value.split('\n').map(line => line.trim()).filter(Boolean);
    const windows = lines.map((line, index) => {
      try {return JSON.parse(line);}
      catch {throw new Error(`第 ${index + 1} 行不是合法 JSON。`);}
    });
    const result = await api('/wash-windows', {dataset_id: datasetId, windows});
    state.status = `已为数据版本 #${datasetId} 登记 ${result.saved} 个洗程窗口。`;
  }, refresh);
}
