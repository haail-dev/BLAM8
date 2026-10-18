"""Render real templates with synthetic, business-neutral browser test evidence."""
from pathlib import Path
import json
import sys
from types import SimpleNamespace as NS

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from jinja2 import Environment,FileSystemLoader
from app import knowledge_review as kr, knowledge_fidelity

env=Environment(loader=FileSystemLoader(ROOT/'src/app/templates'),autoescape=True)
env.globals['csrf_token']=lambda request:'offline-token'
entries=[]
for i,(topic,questions) in enumerate(kr.QUESTIONS.items()):
    label={'prices':'Prices','rules':'Rules','process':'First request','cadence':'Repeat work','payment':'Payment','safety':'Safety','coverage':'Service area','hours':'Hours'}[topic]
    entries.append(dict(id='draft-'+str(i),label=label,content='We provide a scheduled support service for customers. These are fictional test facts.',topics=[topic],mailbox_id=None,
                        provenance=[{'source':'https://example.test/'+topic,'excerpt':'These are fictional test facts.'}]))
row=dict(id='review-fixture',state='pending',payload=dict(entries=entries,origin='Website',return_to='/onboarding/knowledge',questions=[{'topic':'prices','question':'Does the price vary with the size of the request?'}]))
row['payload']['source_units']=knowledge_fidelity.inventory([
    {'url':'https://example.test/rules','text':'These are fictional test facts.\nApproval must precede a booking.'}])
context=dict(version='fixture-version',brief=entries[0]['content'],sources=[],reviews=[row])
empty={**context,'reviews':[]}
request=NS(state=NS(tenant_id='offline',user_email='owner@example.test'),url=NS(path='/onboarding/knowledge'))
base=dict(request=request,asset_rev='offline',step='knowledge',stepper='knowledge',steps=['interview','mailbox','voice','knowledge'],step_count=4,step_index=3,tuning=[],configured=True,coverage_ask='',coverage={},sources=[],site_url='',auto_site=False,pending_reviews=[],review_coverage=kr.topic_view(empty))
html=env.get_template('onboarding/knowledge.html').render(**base)
review=kr.review_view(context,row)
review_html=env.get_template('_knowledge_review.html').render(request=request,review=review)
coverage_html=env.get_template('_knowledge_coverage_review.html').render(review_coverage=kr.topic_view(context))
standalone=env.get_template('knowledge_review.html').render(request=request,asset_rev='offline',review=review,review_coverage=kr.topic_view(context))
print(json.dumps(dict(html=html,standalone=standalone,review_html=review_html,coverage_html=coverage_html,entries=entries,pages=['https://example.test/'+k for k in kr.QUESTIONS],site='https://example.test')))
