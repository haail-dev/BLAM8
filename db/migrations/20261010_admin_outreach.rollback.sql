-- Safety rollback: retain delivery and suppression history permanently.
update admin_outreach_config set enabled=false where id=1;
