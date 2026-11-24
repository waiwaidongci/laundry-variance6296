import {display,esc} from '../../utils/html.js';
import {table} from './table.js';

function chart(spec) {
  const points = (spec?.points || []).filter(point => Number.isFinite(point.value)).slice(0,12);
  if (!points.length) return '';
  const max = Math.max(...points.map(point => Math.abs(point.value)), 1e-9);
  return `<figure><figcaption>${esc(spec.label)} ${spec.unit ? `· ${esc(spec.unit)}` : ''}</figcaption>${points.map(point => {
    const proportion = Math.abs(point.value) / max * 47;
    const start = point.value < 0 ? 50 - proportion : 50;
    return `<div class="chart-row"><span>${esc(point.label)}</span><div class="chart-track"><i style="inset-inline-start:${start}%;width:${proportion}%"></i></div><strong>${esc(display(point.value))}</strong></div>`;
  }).join('')}</figure>`;
}

function reconciliationBlock(reconciliation) {
  if (!reconciliation) return '';
  const versions = Object.entries(reconciliation.version_ids || {});
  const pairRows = (reconciliation.pairings || []).map(pair => ({
    工坊: pair.shop,
    读数编号: pair.reading_id,
    洗涤批号: pair.batch_id,
    读数方量: pair.reading_liters,
    批次实耗: pair.batch_liters,
    水表差: pair.gap,
    状态: pair.status,
  }));
  const summary = {
    确定配对: reconciliation.certain_count,
    争议配对: reconciliation.disputed_count,
    未配读数: reconciliation.unmatched_reading_count,
    未配批次: reconciliation.unmatched_batch_count,
    确定配对水表差合计: reconciliation.gap_total,
  };
  return `<h3>水表对账（分析时刻冻结）</h3>
    <p class="hint">生效版本：${versions.length ? versions.map(([shop, id]) => `${esc(shop)} #${id}`).join('；') : '无'}。报告批准后沿用本快照，随后迟到读数重算不影响已批准报告。</p>
    ${table(Object.entries(summary).map(([key, value]) => ({指标: key, 数值: value})))}
    ${pairRows.length ? table(pairRows) : ''}`;
}

export function report(result) {
  const metrics = (result.metric_order || Object.keys(result.metrics)).map(key => [key,result.metrics[key]]);
  const columns = result.columns?.map(key => ({key,label:key}));
  return `<div class="reading"><h2>${esc(result.headline)}</h2><dl class="metrics">${metrics.map(([key,value]) => `<div><dt>${esc(key)}</dt><dd>${esc(display(value))}</dd></div>`).join('')}</dl>${chart(result.chart)}<h3>业务明细</h3>${table(result.rows,columns)}${reconciliationBlock(result.reconciliation)}<h3>口径与限制</h3><ul>${result.notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul></div>`;
}
