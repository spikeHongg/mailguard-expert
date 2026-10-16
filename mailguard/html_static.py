"""Bounded HTML inspection only: no browser, script runtime, file writes or network."""
import hashlib
import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_HTML_BYTES=512*1024
MAX_TOTAL_BYTES=2*1024*1024
MAX_RECORDS=240
MAX_DEPTH=256
VERSION='html-static-1'
VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
SENSITIVE=re.compile(r'password|passwd|passcode|(?:^|[_\W])pin(?:$|[_\W])|otp|one.time.code|cvv|cvc|credit.card|密码|口令|验证码|网银|银行卡',re.I)
DYNAMIC=re.compile(r'\beval\s*\(|\batob\s*\(|\bunescape\s*\(|document\.write\s*\(|\.innerHTML\s*=|createElement\s*\(',re.I)

class InspectionLimit(ValueError):
    pass


def url_fact(value,base=''):
    result={'literal':value,'resolved':value,'status':'unknown_current_document'}
    if not value:
        return result
    try:
        resolved=urljoin(base,value) if base else value
        parts=urlsplit(resolved)
        result.update(resolved=resolved,scheme=parts.scheme,hostname=parts.hostname)
        result['status']='absolute' if parts.scheme and parts.hostname else 'relative_or_non_http'
    except ValueError:
        result['status']='invalid_url'
    return result


class Inspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]
        self.forms=[];self.controls=[];self.scripts=[];self.handlers=[];self.frames=[]
        self.counts=Counter();self.notes=[];self.labels={};self.text=[];self.title=[]
        self.base='';self.current_form=None;self.label_target=None;self.active_script=False
        self.dynamic=[];self.label_controls=[];self.label_text=[];self.in_label=False

    def append(self,collection,item):
        if len(collection)<MAX_RECORDS:
            collection.append(item)
        elif 'record_limit' not in self.notes:
            self.notes.append('record_limit')

    def handle_starttag(self,tag,attrs):
        if len(self.stack)>=MAX_DEPTH:
            self.notes.append('nesting_limit')
            raise InspectionLimit()
        a={key:value or '' for key,value in attrs}
        if len(a)!=len(attrs):self.notes.append('duplicate_attributes')
        if any(value and len(value)>1024 for value in a.values()):
            self.notes.append('attribute_truncated')
            a={key:value[:1024] if value else value for key,value in a.items()}
        parent_inert=any(x[1]for x in self.stack)
        inert=parent_inert or tag=='template' or 'disabled' in a
        if tag=='base' and not self.base:
            self.base=a.get('href','')
        if tag=='form':
            self.counts['forms']+=1
            if self.current_form is not None:self.notes.append('nested_forms_ambiguous')
            index=f'F{self.counts["forms"]:03d}'
            self.current_form=index
            self.append(self.forms,{'key':index,'id':a.get('id',''),'method':a.get('method','get').lower(),
                                   'action':a.get('action',''),'inert':inert})
        is_control=tag in ('input','textarea','select','button') or 'contenteditable' in a or a.get('role')=='textbox'
        if is_control:
            self.counts['controls']+=1
            typ=(a.get('type') or ('text' if tag=='input' else 'submit' if tag=='button' else tag)).lower()
            self.append(self.controls,{'tag':tag,'type':typ,
                **{k:a.get(k,'')for k in ('id','name','autocomplete','placeholder')},
                'form_attribute':a.get('form'), 'ancestor_form':self.current_form,
                'formaction':a.get('formaction'), 'formmethod':a.get('formmethod'),
                'disabled':inert,'readonly':'readonly' in a,'hidden':a.get('type')=='hidden' or 'hidden'in a,
                'has_prefilled_value':bool(a.get('value')),
                'submission_override_applies':tag in ('input','button') and typ in ('submit','image'),
                'editable':a.get('contenteditable','').lower()!='false' if 'contenteditable' in a else None})
            if self.in_label and self.controls:self.label_controls.append(self.controls[-1])
        if tag=='label':
            self.label_target=a.get('for');self.in_label=True;self.label_controls=[];self.label_text=[]
        if tag=='script':
            self.counts['scripts']+=1
            typ=a.get('type','').lower()
            executable=typ in ('','module','text/javascript','application/javascript')
            self.active_script=executable and not parent_inert
            if a.get('src'):
                self.append(self.scripts,{'src':a['src'],'type':typ or 'javascript','inert':not self.active_script})
        if tag in ('iframe','object','embed'):
            self.counts['embedded_documents']+=1
            self.append(self.frames,{'tag':tag,'src':a.get('src',a.get('data','')),'inert':inert,
                                     'has_srcdoc':'srcdoc' in a})
            if 'srcdoc' in a:self.notes.append('srcdoc_not_recursively_inspected')
        for name,value in attrs:
            if name.startswith('on') and value:
                self.counts['event_handlers']+=1
                # Do not transmit prefilled values or arbitrarily long code snippets.
                self.append(self.handlers,{'element':tag,'event':name,'code_excerpt':value[:240],
                                           'excerpt_truncated':len(value)>240,'inert':inert})
        if tag not in VOID:self.stack.append((tag,inert))

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in VOID:self.handle_endtag(tag)

    def handle_endtag(self,tag):
        if tag=='form':self.current_form=None
        if tag=='label':
            for control in self.label_controls:control['wrapping_label']=' '.join(self.label_text)[:160]
            self.label_target=None;self.in_label=False;self.label_controls=[];self.label_text=[]
        if tag=='script':self.active_script=False
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                self.stack=self.stack[:i];break

    def handle_data(self,data):
        tags={x[0]for x in self.stack}
        if 'script' in tags:
            if self.active_script:
                for match in DYNAMIC.finditer(data):
                    self.append(self.dynamic,{'token':match[0]})
            return
        if tags & {'style','template','textarea','option'}:return
        if 'title'in tags:self.title.append(data)
        if self.label_target:
            self.labels[self.label_target]=(self.labels.get(self.label_target,'')+data)[:160]
        if self.in_label:self.label_text.append(data.strip())
        if sum(map(len,self.text))<2400 and data.strip():self.text.append(data.strip()[:300])

    def result(self):
        by_id={}
        for form in self.forms:
            if form['id'] in by_id and form['id']:self.notes.append('duplicate_form_ids')
            if form['id']:by_id.setdefault(form['id'],form)
            form['destination']=url_fact(form.pop('action'),self.base)
        by_key={x['key']:x for x in self.forms}
        for control in self.controls:
            control['label']=self.labels.get(control['id'],'') or control.pop('wrapping_label','')
            control.pop('wrapping_label',None)
            explicit=control.pop('form_attribute')
            ancestor=control.pop('ancestor_form')
            owner=by_id.get(explicit) if explicit is not None else by_key.get(ancestor)
            control['form_owner']=owner['key'] if owner else None
            if explicit is not None and owner is None:self.notes.append('unresolved_form_owner')
            override=control.pop('formaction')
            control['submission_override']=url_fact(override,self.base) if override is not None else None
            control['sensitive_hint']=bool(control['type'].lower()=='password' or SENSITIVE.search(' '.join(str(control[x]) for x in ('name','id','autocomplete','placeholder','label'))))
        for script in self.scripts:script['destination']=url_fact(script.pop('src'),self.base)
        for frame in self.frames:frame['destination']=url_fact(frame.pop('src'),self.base)
        return {'title':' '.join(self.title)[:200],'visible_excerpt':' '.join(self.text)[:2400],
                'base_url':self.base,'counts':dict(self.counts),'forms':self.forms,'controls':self.controls,
                'external_scripts':self.scripts,'event_handlers':self.handlers,'embedded_documents':self.frames,
                'dynamic_code_markers':self.dynamic,'notes':sorted(set(self.notes))}


def inspect_html(data,source_id,filename='',charset=None,budget=MAX_HTML_BYTES):
    info={'source_id':source_id,'filename':filename,'byte_length':len(data),'sha256':hashlib.sha256(data).hexdigest(),
          'inspector_version':VERSION,'status':'inspected','limitations':['静态提取；未执行脚本、提交表单、访问URL或加载外部资源；不是病毒扫描结果。']}
    if len(data)>min(MAX_HTML_BYTES,budget):
        return {**info,'status':'skipped_size','limitations':info['limitations']+['超过静态检查字节上限，内容未检查。']}
    try:
        if not charset:
            match=re.search(br'charset\s*=\s*["\']?([\w-]+)',data[:2048],re.I)
            charset=match[1].decode('ascii')if match else 'utf-8'
        text=data.decode(charset,errors='replace')
    except (LookupError,UnicodeError):
        return {**info,'status':'decode_failed'}
    parser=Inspector()
    try:
        parser.feed(text);parser.close()
        result=parser.result()
    except InspectionLimit:
        result=parser.result()
    except (ValueError,RecursionError):
        return {**info,'status':'parse_failed'}
    if '\ufffd'in text:
        result['notes'].append('decoding_replacement')
    if result['dynamic_code_markers']:
        result['notes'].append('dynamic_dom_not_executed')
    if result['notes']:info['status']='partial'
    return {**info,**result}


def inspect_message(raw):
    from email import policy
    from email.parser import BytesParser
    msg=BytesParser(policy=policy.default).parsebytes(raw)
    records=[];budget=MAX_TOTAL_BYTES
    for index,part in enumerate(msg.walk()):
        if part.is_multipart():continue
        filename=part.get_filename()or''
        candidate=part.get_content_type()=='text/html' or filename.lower().endswith(('.html','.htm','.xhtml'))
        if not candidate:continue
        data=part.get_payload(decode=True)or b''
        source_id=f'attachment:{index}' if filename or part.get_content_disposition()=='attachment' else f'body:{index}'
        record=inspect_html(data,source_id,filename,part.get_content_charset(),budget)
        records.append(record)
        if record['status'] not in ('skipped_size','decode_failed','parse_failed'):budget=max(0,budget-len(data))
    return records
