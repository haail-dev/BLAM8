"""Representative source overhead without hiding the five real unknown questions."""
import pytest
from app import knowledge_fidelity, knowledge_guidance as guide, knowledge_review
from tests.test_knowledge_review import context, entry


def representative():
    headings=['Residential property management','Home','About','Services','Contact','A little more time for your property',
        'Personal service','Fees and payment','Management fees','Invoices','Repair estimates','Ongoing service',
        'Service area','Reporting repairs','Starting management','Our approach','Get in touch','Useful links',
        'Privacy','Terms']
    notes=[entry('one','Ongoing service',"Inspection visits follow the owner's agreement.")]
    notes += [entry(str(i),label,text) for i,label,text in [
        (2,'Alpha','First distinct confirmed fact.'),(3,'Beta','Second independent instruction.'),
        (4,'Gamma','Third separate contact route.'),(5,'Delta','Fourth isolated process.'),
        (6,'Epsilon','Fifth documented boundary.'),(7,'Zeta','Sixth approved service.'),
        (8,'Eta','Seventh retained detail.'),(9,'Theta','Eighth verified term.')]]
    for note in notes:note['topics']=list(knowledge_review.QUESTIONS)
    ctx,row=context(notes)
    questions=['What are optional-service prices?','What cancellation notice applies?',
        'How frequent are inspections?','Is consolidated invoicing available?',
        'What are the out-of-hours emergency procedures?']
    row['payload']['questions']=[{'topic':topic,'question':q} for topic,q in zip(
        ['prices','rules','cadence','payment','safety'],questions)]
    texts=headings+['Inspection visits follow the owner’s agreement.']
    offset=0;blocks=[]
    for i,text in enumerate(texts):
        blocks.append({'text':text,'start':offset,'end':offset+len(text),'location':'h2' if i<20 else 'body'})
        offset+=len(text)+1
    row['payload']['source_units']=knowledge_fidelity.inventory([{'url':'https://example.test',
        'text':'\n'.join(texts),'blocks':blocks}])
    return ctx,row,{'version':ctx['version']}


@pytest.mark.asyncio
async def test_representative_headings_are_auditable_without_per_label_questions(monkeypatch):
    from app import llm
    async def forbidden(**kwargs):pytest.fail('Deterministic review actions need no model')
    monkeypatch.setattr(llm,'create',forbidden)
    ctx,row,form=representative()
    interactions=0
    while True:
        shown=guide.present(ctx,row,form)
        kind=shown['focus']['kind']
        if kind=='ready':break
        assert kind in {'note','question','source_batch','layout_batch'},shown['focus']
        message={'note':'Keep this note','question':'Leave this unknown','source_batch':'Leave these source details unknown',
                 'layout_batch':'Leave reviewed layout labels in the audit'}[kind]
        result=await guide.turn('offline',ctx,row,form,message,shown['focus_hash'])
        form.update(result['patches']);interactions+=1
        assert interactions<=16
    assert interactions==10  # Removing sector-specific label shortcuts adds no extra turns.
    _,payload=knowledge_review.plan(ctx,row,form)
    assert sum(s['status']=='layout_fragment' for s in payload['source_decisions'])==7
    # The Contact heading is layout, not factual coverage from the isolated
    # word "contact" inside "Third separate contact route."
    assert next(s for s in payload['source_decisions'] if s['text']=='Contact')['status']=='layout_fragment'
    assert all(s['reviewed'] for s in payload['source_decisions'] if s['status']=='layout_fragment')
    assert sum(s['status']=='deferred_unknown' for s in payload['source_decisions'])==13
    assert sum(s['status']=='literal_in_note' for s in payload['source_decisions'])==1
    questions=[q for t in payload['topic_reviews'].values() for q in t['questions']]
    assert len(questions)==4 and all(q['status']=='unknown' for q in questions)
