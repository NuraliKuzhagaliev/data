"""Small non-executing .env reader. Values and parse errors never expose keys."""
from pathlib import Path
from .domain import ValidationError

ENV_FIELDS = {'GROQ_API_KEY':'key','GEMINI_API_KEY':'gemini_key','CQA_PROVIDER':'provider',
              'CQA_MODEL':'model','CQA_TEAM_NAME':'team_name',
              'CQA_GROQ_FREE_PLAN_CONFIRMED':'free_plan_confirmed',
              'CQA_GEMINI_FREE_TIER_CONFIRMED':'gemini_free_plan_confirmed'}
BOOL_FIELDS = {'free_plan_confirmed','gemini_free_plan_confirmed'}


def environment_config(values):
    result={}
    for name,field in ENV_FIELDS.items():
        value=values.get(name)
        if value is None or value=='':continue
        if field in BOOL_FIELDS:
            if value.lower() not in ('true','false'):raise ValidationError(name+': используйте true или false')
            result[field]=value.lower()=='true'
        elif field=='provider':
            if value not in ('groq','gemini','llamacpp','ollama'):raise ValidationError('CQA_PROVIDER: неизвестный провайдер')
            result[field]=value
        else:result[field]=value
    return result


def dotenv_config(path):
    path=Path(path)
    if not path.exists():return {}
    if path.stat().st_size>64000:raise ValidationError('.env слишком большой')
    values={}
    for line_number,line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
        line=line.strip()
        if not line or line.startswith('#'):continue
        if line.startswith('export '):line=line[7:].strip()
        if '=' not in line:raise ValidationError(f'.env: ожидается NAME=value в строке {line_number}')
        name,value=line.split('=',1);name=name.strip();value=value.strip()
        if name not in ENV_FIELDS:continue
        if value.startswith(('"',"'")):
            if len(value)<2 or value[-1]!=value[0]:raise ValidationError(f'.env: незакрытая кавычка в строке {line_number}')
            value=value[1:-1]
        else:value=value.split(' #',1)[0].rstrip()
        values[name]=value
    return environment_config(values)
