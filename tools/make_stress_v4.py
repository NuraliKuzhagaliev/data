"""Development stress protocol clarified from checklist N/A priority, not human GT."""
import json
from pathlib import Path

records=json.loads(Path('.local/stress-cases.json').read_text(encoding='utf-8'))['records']
records += [
    {"conversation_id":"stress-clarification-needed-v4","language":"RU","source":"synthetic","channel":"agent chat","transcript":[
        {"timestamp":"00:00","speaker":"client","text":"Здравствуйте! Не могу разобраться со списанием, помогите."},
        {"timestamp":"00:05","speaker":"operator","text":"Здравствуйте! Меня зовут Мария. Уточните, пожалуйста, это списание при пополнении счёта или комиссия из уведомления?"},
        {"timestamp":"00:10","speaker":"client","text":"Это комиссия из уведомления, а не пополнение."},
        {"timestamp":"00:15","speaker":"operator","text":"Поняла, вопрос о комиссии из уведомления. Для разбора конкретной операции обратитесь в поддержку через защищённый канал приложения и приложите уведомление. Не отправляйте личные данные в общий чат. Могу ещё помочь? До свидания!"}]},
    {"conversation_id":"stress-bot-greeting-v4","language":"EN","source":"synthetic","channel":"agent chat","transcript":[
        {"timestamp":"00:00","speaker":"client","text":"Where can I find the general FAQ? I do not need any account-specific information."},
        {"timestamp":"00:01","speaker":"bot","text":"Hello! My name is AIM, the virtual assistant. I will transfer you to an operator."},
        {"timestamp":"00:05","speaker":"operator","text":"Open the Help section on the official website and choose FAQ for general questions."},
        {"timestamp":"00:10","speaker":"client","text":"Thank you, that is enough."},
        {"timestamp":"00:15","speaker":"operator","text":"To recap, open Help and then FAQ on the official website. Please let us know if you need more help. Goodbye!"}]}
]
targets=json.loads(Path('evidence/stress-protocol.json').read_text(encoding='utf-8'))['targets']
for cid in ('stress-ru-good','stress-ru-no-contact','stress-ru-injection','stress-en-good','stress-kz-good'):
    targets[cid]['discovery']=None
targets['stress-private-paid-offer']['contact']=True
targets['stress-clarification-needed-v4']={'contact':True,'discovery':True}
targets['stress-bot-greeting-v4']={'contact':False,'discovery':None}
Path('.local/stress-v4.json').write_text(json.dumps({'records':records},ensure_ascii=False,indent=2),encoding='utf-8')
Path('evidence/stress-protocol-v4.json').write_text(json.dumps({'label_source':'synthetic','protocol':'v4-development','targets':targets,'note':'N/A priority and independence clarified from checklist pp.1-3 after development errors. This is development stress testing, not a held-out accuracy benchmark or human Ground Truth.'},ensure_ascii=False,indent=2),encoding='utf-8')
print('8 authored development stress cases; human GT not created')
