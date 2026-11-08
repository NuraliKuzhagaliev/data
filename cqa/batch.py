"""Resumable CLI processing with an immutable attempt log and full saved plan."""
import json
import time
import uuid
from pathlib import Path
from .domain import ValidationError, validate_decisions
from .providers import ProviderError
from .prompts import prompt_hash
from .rubric import official_rubric
from .store import now, source_hash


def atomic_json(path, value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def run_batch(app, records, provider, target, *, kind, mode='evidence', repeat=1, resume=False, emit=print):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    rubric=app.store.rubric(official_rubric()['id']);code=source_hash()
    signature={'provider':kind,'requested_model':provider.model,'mode':mode,'prompt_hash':prompt_hash(mode),'rubric_hash':rubric['hash'],'code_hash':code}
    plan=[(c,n) for n in range(repeat) for c in records]
    previous=json.loads(target.read_text(encoding='utf-8')) if resume and target.exists() else []
    if not isinstance(previous,list):raise ValidationError('Для --resume нужен массив сохранённых оценок')
    if any(not isinstance(row,dict) for row in previous):raise ValidationError('Сохранённые оценки должны быть объектами')
    key=lambda row:(row.get('external_conversation_id',row.get('conversation_id')),row.get('repeat',0))
    cached={};sources={c['conversation_id']:c for c in records}
    for row in previous:
        if row.get('status','ok')!='ok' or key(row)[0] not in sources:continue
        c=sources[key(row)[0]];meta=row.get('provenance',{})
        actual={**meta,'requested_model':meta.get('requested_model',meta.get('model'))}
        if row.get('transcript_hash')!=c['hash'] or any(actual.get(k)!=v for k,v in signature.items()):
            raise ValidationError('--resume: источник, код или параметры отличаются от успешной оценки. Используйте новый --output; для смены провайдера только у ошибок доступно продолжение пакета в UI')
        validate_decisions({'criteria':row['criteria']},rubric,c)
        if key(row) in cached:raise ValidationError('В сохранённом файле повторяется успешная оценка')
        cached[key(row)]=row
    if target.exists():
        backup=target.with_name(target.name+'.previous-'+uuid.uuid4().hex[:10]+'.json')
        backup.write_bytes(target.read_bytes())
    completed={k:v for k,v in cached.items() if k in {(c['conversation_id'],n) for c,n in plan}}
    plan_path=target.with_suffix(target.suffix+'.plan.json');attempt_path=target.with_suffix(target.suffix+'.attempts.jsonl')
    status={'created_at':now(),'status':'running','signature':signature,'items':[{'conversation_id':c['conversation_id'],'transcript_hash':c['hash'],'repeat':n} for c,n in plan]}
    def persist():
        rows=[completed[(c['conversation_id'],n)] for c,n in plan if (c['conversation_id'],n) in completed]
        atomic_json(target,rows)
        status.update(completed=len(rows),total=len(plan),updated_at=now())
        atomic_json(plan_path,status)
    persist()  # Preserve even future cached successes before the first HTTP call.
    try:
        for conversation,n in plan:
            identity=(conversation['conversation_id'],n)
            if identity in cached:
                emit(json.dumps({'id':identity[0],'status':'reused','repeat':n},ensure_ascii=False));continue
            started=time.perf_counter();metadata={**signature,'model':provider.model,'is_mock':False}
            try:
                raw,received=provider.evaluate(rubric,conversation,mode=mode)
                metadata.update(received);metadata['requested_model']=provider.model
                result=app.store.save_evaluation(conversation,rubric,raw,metadata);result['repeat']=n
            except Exception as exc:
                metadata.update(getattr(exc,'metadata',{}));metadata['requested_model']=provider.model
                metadata.setdefault('latency_ms',round((time.perf_counter()-started)*1000,2))
                error=str(exc) if isinstance(exc,(ValidationError,ProviderError)) else type(exc).__name__
                result={'conversation_id':conversation['conversation_id'],'status':'error','error':error,'repeat':n,
                        'error_code':getattr(exc,'code','validation' if isinstance(exc,ValidationError) else 'unexpected'),
                        'retry_after':getattr(exc,'retry_after',None),'provenance':metadata}
                app.store.record_failure(conversation,rubric,error,metadata,getattr(exc,'raw_response',None))
            completed[identity]=result
            with attempt_path.open('a',encoding='utf-8') as log:log.write(json.dumps(result,ensure_ascii=False)+'\n')
            persist()
            emit(json.dumps({'id':identity[0],'status':result.get('status','ok'),'repeat':n,'completed':status['completed'],'total':status['total']},ensure_ascii=False))
            if result.get('error_code') in ('quota','rate_limit') and (metadata.get('daily_quota') or (result.get('retry_after') or 0)>55):
                status.update(status='interrupted',reason='provider_quota');break
        else:status['status']='completed'
    except BaseException:
        status['status']='interrupted';raise
    finally:persist()
    values=list(completed.values())
    return 0 if len(values)==len(plan) and all(r.get('status','ok')=='ok' for r in values) else 2
