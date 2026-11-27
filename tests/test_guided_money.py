from app import guided_setup, knowledge_review, onboarding_contract, onboarding_state


def test_money_shows_current_prices_without_creating_a_price_list():
    state = onboarding_contract.neutral_state()
    question = dict(id='knowledge.rates', kind='text', scopes=['organisation'], topic='prices',
                    text=knowledge_review.QUESTIONS['prices'][0], substep='charges.terms',
                    capability='charging', adapter=None, dependencies=[])
    state['routed_questions'] = {question['id']: question}
    sources = [dict(source='Prices', content='A call-out costs £25.')]
    context = dict(state=state, knowledge=dict(sources=sources, reviews=[]))
    stop = dict(substep='charges.terms', question_id=question['id'], kind='fact')
    assert guided_setup.price_reference(context, stop) == dict(text=sources[0]['content'], saved=False, current=True)
    sources.append(dict(source='Extra prices', content='There is another call-out price.'))
    assert guided_setup.price_reference(context, stop) is None
    assert not state['answers']
    onboarding_state._confirmed_answer(state, question, 'organisation', 'organisation', 'answered', '£35 per visit', 'owner')
    preview = dict(substep='charges.terms', kind='decision', href='/app/invoices/settings')
    assert guided_setup.price_reference(context, preview) == dict(text='£35 per visit', saved=True, current=False)
    assert not guided_setup.review_blocker(state, 'charges.terms')
    onboarding_state._confirmed_answer(state, question, 'organisation', 'organisation', 'deferred', None, 'owner')
    assert guided_setup.price_reference(context, preview) is None
    assert guided_setup.review_blocker(state, 'charges.terms')


def test_finished_guide_opens_home_after_final_review():
    state = onboarding_contract.neutral_state()
    state['guide'] = guided_setup.neutral()
    context = dict(state=state, modules={'crm', 'scheduling'}, cfg={}, groups=[], invoice={}, config={})
    assert guided_setup.steps(context)[-1]['href'] == '/app/setup/charges'
    state['guide']['status'] = 'finished'
    assert guided_setup.steps(context)[-1]['href'] == '/app/'
