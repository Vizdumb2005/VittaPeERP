import frappe
from frappe.model.document import Document


class ApprovalRequest(Document):
	def validate(self):
		if self.status in {"Approved", "Rejected"} and not self.checker:
			frappe.throw("Checker is required when approving or rejecting.")


@frappe.whitelist()
def review(name: str, action: str, reason: str | None = None):
	"""Checker action for Approval Request."""
	action = (action or "").strip().lower()
	if action not in {"approve", "reject"}:
		frappe.throw("action must be either approve or reject")

	doc = frappe.get_doc("Approval Request", name)

	if frappe.session.user != doc.checker:
		frappe.throw("Only assigned checker can review this request.")

	doc.status = "Approved" if action == "approve" else "Rejected"
	doc.reason = reason
	doc.save(ignore_permissions=True)

	return {"name": doc.name, "status": doc.status}
