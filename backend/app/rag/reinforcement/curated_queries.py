"""Curated golden-set pairs — hand-written, pipeline-independent.

The feedback-derived golden set has a selection bias: +1 rows can only come
from queries where the CURRENT pipeline already surfaced the right symbol, so
any ranking change tends to look worse by definition (the reranker A/B that
showed MRR 0.49 -> 0.395 was measured against that biased set). These pairs
are written from domain knowledge WITHOUT running the pipeline, and each
query deliberately avoids the symbol's own name so the set is not lexically
skewed (measured: the feedback set is ~3% name-match-heavy; these aim for 0%).

Nonexistent symbol ids drop out silently at build time (has_symbol check) —
if a rename lands, the pair disappears rather than scoring as a miss.
"""

CURATED_GOLDEN_PAIRS: list[dict[str, str]] = [
    # payments / wallets
    {"query": "deciding how to fund a charge across two wallet types",
     "symbol_id": "dashboard::product::core::combined_wallet_debit_core::prepare_combined_debit"},
    {"query": "moving money from the main wallet to the prepaid one",
     "symbol_id": "dashboard::product::core::combined_wallet_debit_core::transfer_from_master_to_ppi"},
    {"query": "checking both wallets together can cover a debit",
     "symbol_id": "dashboard::product::core::combined_wallet_debit_core::validate_combined_wallet_balance"},
    {"query": "core logic that routes a charge between wallet and payment gateway",
     "symbol_id": "dashboard::product::controller::payment_controller::process_transaction_core"},
    {"query": "constructing the bank payment client from configuration",
     "symbol_id": "dashboard::integrations::payment_gateways::factory::build_payment_gateway"},
    {"query": "trait every bank integration implements",
     "symbol_id": "dashboard::integrations::payment_gateways::provider::PaymentGateway"},
    {"query": "background job that registers customer vehicles",
     "symbol_id": "dashboard::product::controller::jobs_controller::process_vehicle_registration_job"},

    # fastag lifecycle
    {"query": "shutting down a toll tag",
     "symbol_id": "fastag::product::core::fastag_core::close_tag"},
    {"query": "giving money back when a tag swap is refused",
     "symbol_id": "dashboard::product::core::fastag_replacement_core::process_replacement_refund"},
    {"query": "steps after approving a replacement: new tag, close old, register",
     "symbol_id": "dashboard::product::controller::fastag_replacement_controller::execute_approval_flow"},
    {"query": "checking if a tag has an open replacement or closure request",
     "symbol_id": "dashboard::routes::merchant_handlers::replacement_eligibility_handler"},
    {"query": "setting up the fake NETC endpoints for testing",
     "symbol_id": "fastag::mock::netc_mock_service::main::configure_mock_service"},
    {"query": "starting the fake bank server",
     "symbol_id": "crates::kotak::mock_server::lib::run"},

    # review workflows
    {"query": "approving a customer's vehicle details change",
     "symbol_id": "dashboard::product::helpers::vehicle_update_helper::approve_vehicle_update"},
    {"query": "reviewer accepts or rejects a vehicle change",
     "symbol_id": "dashboard::product::controller::vehicle_update_controller::approve_or_reject_vehicle_update"},

    # validation / document handling
    {"query": "checking an uploaded file is really the type it claims",
     "symbol_id": "dashboard::utils::file_type_validation::validate_file_content"},
    {"query": "magic byte verification",
     "symbol_id": "dashboard::utils::file_type_validation::verify_content"},
    {"query": "validating a document already uploaded to object storage",
     "symbol_id": "dashboard::product::controller::s3_document_controller::validate_uploaded_document"},

    # concurrency / integrity
    {"query": "preventing two flows from touching the same customer record",
     "symbol_id": "fastag::product::core::entity_locked::acquire_lock_if_eligible"},
    {"query": "finding locks that conflict with a new operation",
     "symbol_id": "fastag::storage::queries::resource_locks_queries::find_active_locks_for_source"},
    {"query": "detecting typos in reference numbers",
     "symbol_id": "crates::common::algorithms::damm::calculate_check_digit"},
    {"query": "comparing the live database against declared tables",
     "symbol_id": "crates::schema-verifier::lib::verify_schema_definitions"},
    {"query": "health endpoint that reports schema mismatches",
     "symbol_id": "dashboard::routes::health::schema_verification_check"},

    # privacy
    {"query": "deciding if a struct field holds personal data",
     "symbol_id": "crates::pii_guard::macros::utils::classify_field"},
    {"query": "pairing encrypted columns with their blind index",
     "symbol_id": "crates::pii_guard::macros::sea_orm::pii_entity::find_paired_fields"},
]
