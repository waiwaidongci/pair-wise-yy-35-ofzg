from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# 修订链：仪器证书 / 原始读数 / 剂量事件
ENTITY_INSTRUMENT='仪器'; ENTITY_CERTIFICATE='校准证书'; ENTITY_READING='原始读数'; ENTITY_BATCH='重算批次'; ENTITY_TODO='待办'
TODO_KINDS=['investigation','follow_up','deadline','conclusion_invalid']
TODO_STATUS=['open','done']
BATCH_ITEM_STATUS=['pending','running','completed','failed','skipped']
BATCH_STATUS=['running','completed','failed']
REVISION_REASONS=['initial','certificate_reissue','historical_baseline','reading_added']
INSTRUMENT_WRITE_ROLES=set(['radiation_officer','health_physicist'])
CERTIFICATE_WRITE_ROLES=set(['radiation_officer','health_physicist'])
READING_WRITE_ROLES=set(['dosimetrist','radiation_officer','health_physicist'])
RECALC_ROLES=set(['radiation_officer','health_physicist'])
TODO_CLOSE_ROLES=set(['radiation_officer','health_physicist'])
BASELINE_UPGRADE_ROLES=set(['radiation_officer','health_physicist'])
CONCLUDED_STATES=set(['investigation','follow_up','closed'])
EPSILON=1e-9
def validate_certificate_interval(effective_from,effective_to):
    if not isinstance(effective_from,str) or not effective_from.strip(): raise ValidationError("生效区间起始不能为空")
    if effective_to is not None and (not isinstance(effective_to,str) or effective_to<=effective_from):
        raise ValidationError("生效区间结束必须晚于起始")
    return effective_from, effective_to
def validate_coefficient(coefficient):
    if isinstance(coefficient,bool): raise ValidationError("校准系数必须是数字")
    try: value=float(coefficient)
    except (TypeError,ValueError): raise ValidationError("校准系数必须是数字")
    if value<=0: raise ValidationError("校准系数必须大于0")
    return value
def todo_kind_label(kind):
    return {'investigation':'调查重估','follow_up':'医学随访重估','deadline':'报告期限变更','conclusion_invalid':'旧结论失效'}[kind]
