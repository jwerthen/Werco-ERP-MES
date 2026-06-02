from app.models.governance import (
    DEFAULT_RETENTION_POLICIES,
    ClassificationReview,
    ClassificationReviewType,
    ControlledAccessEvent,
    CustomerContract,
    CustomerHandlingInstruction,
    DataClassification,
    DocumentFile,
    ExportEvent,
    HandlingGroup,
    HandlingGroupMember,
    HandlingGroupRole,
    LegalHold,
    RetentionPolicy,
)


def test_default_retention_policies_are_unique_and_seedable() -> None:
    policy_keys = [policy["policy_key"] for policy in DEFAULT_RETENTION_POLICIES]

    assert len(policy_keys) == len(set(policy_keys))
    assert "controlled_document" in policy_keys
    assert "security_audit_record" in policy_keys
    assert "temporary_import_processing" in policy_keys


def test_governance_foundation_records_can_be_persisted(db_session, test_user) -> None:
    policy = RetentionPolicy(
        company_id=1,
        policy_key="controlled_document",
        name="Controlled Document",
        retention_basis="Life of part or program plus 10 years.",
        retention_trigger="part_or_program_end",
        applies_to_record_types=["documents", "document_files"],
    )
    db_session.add(policy)
    db_session.flush()

    contract = CustomerContract(
        company_id=1,
        contract_number="CMMC-TEST-001",
        contract_name="CMMC Test Contract",
        default_data_classification=DataClassification.CUI.value,
        contains_cui=True,
        retention_policy_id=policy.id,
    )
    db_session.add(contract)
    db_session.flush()

    instruction = CustomerHandlingInstruction(
        company_id=1,
        contract_id=contract.id,
        instruction_type="marking",
        instruction_text="Apply controlled markings to exports.",
        default_data_classification=DataClassification.CUI.value,
        requires_marking=True,
        requires_export_review=True,
        retention_policy_id=policy.id,
    )
    db_session.add(instruction)

    handling_group = HandlingGroup(
        company_id=1,
        name="CUI Reviewers",
        classification_scope=DataClassification.CUI.value,
        contract_id=contract.id,
    )
    db_session.add(handling_group)
    db_session.flush()

    membership = HandlingGroupMember(
        company_id=1,
        handling_group_id=handling_group.id,
        user_id=test_user.id,
        membership_role=HandlingGroupRole.REVIEWER.value,
        approved_by=test_user.id,
    )
    db_session.add(membership)

    file_record = DocumentFile(
        company_id=1,
        parent_record_type="documents",
        parent_record_id=123,
        storage_provider="supabase_storage",
        storage_container="documents",
        storage_key="documents/123/rev-a/test.pdf",
        original_file_name="test.pdf",
        file_size=1024,
        mime_type="application/pdf",
        content_sha256="a" * 64,
        file_classification=DataClassification.CUI.value,
        retention_policy_id=policy.id,
        uploaded_by=test_user.id,
    )
    db_session.add(file_record)
    db_session.flush()

    review = ClassificationReview(
        company_id=1,
        record_type="documents",
        record_id=123,
        previous_classification=DataClassification.UNKNOWN.value,
        new_classification=DataClassification.CUI.value,
        review_type=ClassificationReviewType.ASSIGNMENT.value,
        reviewed_by=test_user.id,
        justification="Customer drawing marked CUI.",
    )
    db_session.add(review)

    legal_hold = LegalHold(
        company_id=1,
        record_type="documents",
        record_id=123,
        hold_reason="Customer dispute hold.",
        hold_owner="Quality",
        placed_by=test_user.id,
    )
    db_session.add(legal_hold)

    export_event = ExportEvent(
        company_id=1,
        record_type="documents",
        record_id=123,
        export_type="download",
        export_format="pdf",
        data_classification=DataClassification.CUI.value,
        generated_file_id=file_record.id,
        exported_by=test_user.id,
        export_reason="Controlled review packet.",
    )
    db_session.add(export_event)

    access_event = ControlledAccessEvent(
        company_id=1,
        record_type="documents",
        record_id=123,
        file_id=file_record.id,
        action="download",
        allowed=True,
        user_id=test_user.id,
        data_classification=DataClassification.CUI.value,
    )
    db_session.add(access_event)

    db_session.commit()

    assert policy.active is True
    assert contract.contains_cui is True
    assert instruction.requires_export_review is True
    assert handling_group.members[0].membership_role == HandlingGroupRole.REVIEWER.value
    assert file_record.file_classification == DataClassification.CUI.value
    assert legal_hold.active is True
    assert export_event.generated_file_id == file_record.id
    assert access_event.allowed is True
