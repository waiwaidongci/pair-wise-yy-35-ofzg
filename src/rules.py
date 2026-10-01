from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
# 医学随访判定阈值：剂量达到调查水平的2倍，或定级本身为high/critical
FOLLOWUP_RATIO=2.0
# 修订后需要重新评估的结论与待办类型
FINDING_KINDS=('investigation','medical_follow_up','report_deadline')
TODO_KINDS=('reassess_investigation','reassess_follow_up','report_deadline_changed')
# 规则效果 -> (受影响的结论类型, 待办类型)
EFFECT_FINDING={'investigation_required':'investigation','medical_followup_required':'medical_follow_up','deadline_hours':'report_deadline'}
EFFECT_TODO={'investigation_required':'reassess_investigation','medical_followup_required':'reassess_follow_up','deadline_hours':'report_deadline_changed'}
# 状态流转时落地的结论
TRANSITION_FINDING={'investigation':('investigation','调查成立：剂量达到调查水平，启动超限调查'),'follow_up':('medical_follow_up','医学随访成立：剂量达到随访水平，安排医学随访'),'closed':('report_deadline','报告期限满足，事件按期关闭')}
# 修订原因
REASON_INITIAL='initial_reading'; REASON_ATTACHED='reading_attached'; REASON_BASELINE='legacy_baseline'; REASON_RECALC='recalculation'
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
def medical_followup_required(severity,quantity=0.0,threshold=1.0):
    """剂量达到调查水平FOLLOWUP_RATIO倍，或定级为high/critical时必须医学随访。"""
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return severity in ('high','critical') or ratio>=FOLLOWUP_RATIO
def rule_effects(severity,quantity=0.0,threshold=1.0):
    """给定剂量下三类管理结论的完整效果（调查/随访/报告期限小时数）。"""
    return {'investigation_required':escalation_required(severity,quantity,threshold),'medical_followup_required':medical_followup_required(severity,quantity,threshold),'deadline_hours':response_deadline_hours(severity,quantity,threshold)}
def changed_effects(before,after):
    """比较修订前后规则效果，仅返回发生变化的效果及新旧值。布尔判定翻转、期限小时数变化均视为结论失效。"""
    changes={}
    for key in ('investigation_required','medical_followup_required','deadline_hours'):
        if before.get(key)!=after.get(key): changes[key]={'before':before.get(key),'after':after.get(key)}
    return changes
def certificate_applies(valid_from,valid_to,measured_at):
    """证书生效区间覆盖测量时刻（valid_to为NULL表示持续有效）。"""
    if measured_at<valid_from: return False
    if valid_to is not None and measured_at>valid_to: return False
    return True
