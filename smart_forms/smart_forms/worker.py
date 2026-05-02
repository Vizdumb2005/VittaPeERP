import frappe
from frappe.utils.pdf import get_pdf


def generate_submission_pdf(submission_name: str):
    """Render form template and attach generated PDF to submission."""
    submission = frappe.get_doc("Form Submission", submission_name)
    smart_form = frappe.get_doc("Smart Form", submission.form)

    context = {
        "submission": submission,
        "form": smart_form,
        "data": frappe.parse_json(submission.data_json) if submission.data_json else {},
    }

    rendered_html = frappe.render_template(smart_form.print_format or "", context)
    pdf_bytes = get_pdf(rendered_html)

    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": f"{submission.name}.pdf",
            "is_private": 1,
            "content": pdf_bytes,
            "attached_to_doctype": "Form Submission",
            "attached_to_name": submission.name,
        }
    )
    file_doc.save(ignore_permissions=True)

    submission.db_set("signed_pdf", file_doc.file_url)
    frappe.db.commit()

    return file_doc.name
