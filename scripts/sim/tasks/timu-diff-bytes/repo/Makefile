# The tests that run inside timu's sandbox. The other modules need the network, a
# terminal or a nested sandbox. src/ comes first on the path, before any installed timu.
PYTHON ?= python3
TESTS = tests/test_agent.py tests/test_config.py tests/test_delegate.py \
	tests/test_events.py tests/test_fs.py tests/test_graph.py tests/test_package.py \
	tests/test_role.py tests/test_skills.py tests/test_types.py tests/test_workflow.py \
	tests/test_review_fixes.py
HOST_ONLY = test_clipboard_is_denied test_a_rate_limit_with_token_wording_is_retried \
	test_a_trickling_response_hits_the_total_limit test_provider_does_not_follow_redirects \
	test_web_drops_credentials_on_a_cross_origin_redirect

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q -p no:cacheprovider $(TESTS) $(EXTRA) \
		$(foreach t,$(HOST_ONLY),--deselect tests/test_review_fixes.py::$(t))

.PHONY: test
