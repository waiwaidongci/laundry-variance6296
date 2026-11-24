export const roles = {analyst: '分析员', reviewer: '复核员', viewer: '只读访客'};
export const states = {queued: '排队', running: '计算中', completed: '已计算', failed: '需修正'};
export const decisions = {approved: '已批准', returned: '已退回'};
export const actions = {import:'导入数据',queue:'提交计算',completed:'完成计算',failed:'计算失败',approved:'批准发布',returned:'退回结果',schedule:'建立计划',cancel:'停用计划',delivered:'报告入箱',window_sync:'同步洗程窗口',meter_queue:'排队水表重算',meter_version:'生成对账版本',meter_failed:'对账重算失败',meter_stale:'旧重算作废'};
export const meterStates = {queued: '排队', running: '重算中', completed: '已完成', failed: '失败保留旧版', stale: '旧结果作废'};
export const versionStates = {active: '生效中', superseded: '已被取代'};
export const pairStatus = {'确定配对': '确定配对', '争议配对': '争议配对'};
