import frappe


@frappe.whitelist(methods=["POST"])
def render_pdf(submission: str):
    """Enqueue PDF generation for a Form Submission."""
    if not submission:
        frappe.throw("submission is required")

    frappe.get_doc("Form Submission", submission)

    job = frappe.enqueue(
        "smart_forms.smart_forms.worker.generate_submission_pdf",
        queue="long",
        submission_name=submission,
        enqueue_after_commit=True,
    )

    return {"status": "queued", "job_id": getattr(job, "id", None), "submission": submission}
