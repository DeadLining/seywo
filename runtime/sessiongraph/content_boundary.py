"""Typed, source-addressed text projection; never decode media or rewrite evidence.

Full source is inspected BEFORE batching, including replay of legacy descriptors.
JSON offsets address the indexed original. Unknown textual output stays intact.
"""
import json
import re

SELECTION_VERSION = 'typed-content-v3'
DATA_URI = re.compile(r'data:[a-zA-Z0-9.+/-]+(?:;[a-zA-Z0-9=.+-]+)*;base64,(?:[A-Za-z0-9+/=_-]|\\/|\\[rn])+',re.I)
MEDIA = {'image','input_image','image_url','audio','input_audio','output_audio','video','input_video','file','input_file','document'}
BINARY_KEYS = {'data','base64','b64_json','file_data','audio_data','image_data','bytes'}
META_KEYS = {'text','transcript','caption','description','title','name','filename','file_name','path','file_path','url','uri','image_url','file_id','mime_type','media_type'}
TOKEN = re.compile(r'\s*("(?:[^"\\]|\\.)*"|[{}\[\],:]|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)',re.S)


def _tree(text):
    """Small offset-preserving JSON reader. json.loads validates scalars/escapes."""
    tokens=list(TOKEN.finditer(text));i=0
    def value(depth=0):
        nonlocal i
        if depth>100 or i>=len(tokens):raise ValueError('invalid content envelope')
        m=tokens[i];i+=1;raw=m[1];start=m.start(1)
        if raw in ('{','['):
            children=[];close='}' if raw=='{' else ']'
            if i<len(tokens) and tokens[i][1]==close:
                end=tokens[i].end(1);i+=1;return ({} if raw=='{' else [],start,end,children)
            while True:
                key=None
                if raw=='{':
                    key=json.loads(tokens[i][1]);i+=1
                    if not isinstance(key,str) or tokens[i][1]!=':':raise ValueError('invalid key')
                    i+=1
                child=value(depth+1);children.append((key,child))
                sep=tokens[i];i+=1
                if sep[1]==close:break
                if sep[1]!=',':raise ValueError('invalid separator')
            obj={k:v[0] for k,v in children} if raw=='{' else [v[0] for _,v in children]
            return obj,start,sep.end(1),children
        return json.loads(raw),start,m.end(1),[]
    # Reject token gaps, malformed JSON and trailing content, not just a prefix.
    end=0
    for token in tokens:
        if token.start()!=end:raise ValueError('invalid token')
        end=token.end()
    if text[end:].strip():raise ValueError('trailing content')
    root=value()
    if i!=len(tokens):raise ValueError('trailing tokens')
    return root


def excluded_spans(text, *, carriers_only=False):
    """Known binary/media carrier ranges; text and attachment references survive."""
    excluded=[[m.start(),m.end()] for m in DATA_URI.finditer(text)]
    if not text.lstrip().startswith(('{','[')):return excluded
    try:root=_tree(text)
    except (ValueError,IndexError,RecursionError):return excluded
    def walk(node,media=False):
        obj,start,end,children=node
        if isinstance(obj,dict):
            kind=obj.get('type',obj.get('kind',''))
            typed=isinstance(kind,str) and kind.lower() in MEDIA
            media=media or typed or obj.get('encoding')=='base64' or obj.get('type')=='base64'
            if typed and not carriers_only:
                # Replace the entire media block with exact metadata value spans,
                # not JSON braces/keys or invented attachment descriptions.
                keep=[]
                def metadata(n, key=''):
                    v,a,b,kids=n
                    if key in META_KEYS and isinstance(v,str):
                        if not DATA_URI.search(v):keep.append([a+1,b-1])
                    elif isinstance(v,(dict,list)):
                        for k,ch in kids:metadata(ch,k or key)
                for key,child in children:metadata(child,key)
                cursor=start
                for a,b in sorted(keep):
                    if cursor<a:excluded.append([cursor,a])
                    cursor=max(cursor,b)
                if cursor<end:excluded.append([cursor,end])
                return
            for key,child in children:
                v,a,b,_=child
                if media and key in BINARY_KEYS and isinstance(v,str):
                    excluded.append([a,b])
                elif not carriers_only and typed and key not in META_KEYS and key not in {'source','content'}:
                    excluded.append([a,b])
                else:walk(child,media)
        elif isinstance(obj,list):
            for _,child in children:walk(child,media)
    walk(root)
    return excluded


def assert_text_only(value):
    """Last-mile guard shared by providers; do not send an unsafe request."""
    if isinstance(value,str):
        if excluded_spans(value, carriers_only=True):
            from .failures import Failure
            raise Failure('model_input_contains_binary', stage='input_boundary')
    elif isinstance(value,dict):
        # Inspect serialized typed envelopes as well as nested textual evidence.
        if excluded_spans(json.dumps(value,ensure_ascii=False), carriers_only=True):
            from .failures import Failure
            raise Failure('model_input_contains_binary', stage='input_boundary')
        for v in value.values():assert_text_only(v)
    elif isinstance(value,(list,tuple)):
        for v in value:assert_text_only(v)
