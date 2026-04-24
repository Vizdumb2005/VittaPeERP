# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


import json
from collections import defaultdict

import frappe
from frappe import _, bold
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder import DocType
from frappe.query_builder.functions import Sum
from frappe.utils import (
	cint,
	comma_or,
	cstr,
	flt,
	format_time,
	formatdate,
	get_link_to_form,
	getdate,
	nowdate,
)

import erpnext
from erpnext.accounts.general_ledger import process_gl_map
from erpnext.accounts.utils import get_account_currency
from erpnext.buying.utils import check_on_hold_or_closed_status
from erpnext.controllers.taxes_and_totals import init_landed_taxes_and_totals
from erpnext.manufacturing.doctype.bom.bom import (
	add_additional_cost,
	get_op_cost_from_sub_assemblies,
	get_secondary_items_from_sub_assemblies,
	validate_bom_no,
)
from erpnext.setup.doctype.brand.brand import get_brand_defaults
from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
from erpnext.stock.doctype.item.item import get_item_defaults
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
from erpnext.stock.doctype.stock_entry.manufacturing_handler import (
	BaseManufacturingHandler,
	DisassembleHandler,
	ManufactureHandler,
	MaterialTransferHandler,
	StockEntrySABB,
)
from erpnext.stock.get_item_details import (
	ItemDetailsCtx,
	get_barcode_data,
	get_bin_details,
	get_conversion_factor,
	get_default_cost_center,
)
from erpnext.stock.serial_batch_bundle import (
	SerialBatchCreation,
	get_batch_nos,
	get_empty_batches_based_work_order,
	get_serial_or_batch_items,
)
from erpnext.stock.stock_ledger import get_previous_sle, get_valuation_rate
from erpnext.stock.utils import get_bin, get_combine_datetime, get_incoming_rate


class FinishedGoodError(frappe.ValidationError):
	pass


class IncorrectValuationRateError(frappe.ValidationError):
	pass


class MaxSampleAlreadyRetainedError(frappe.ValidationError):
	pass


from erpnext.controllers.stock_controller import StockController
from erpnext.controllers.subcontracting_inward_controller import SubcontractingInwardController

form_grid_templates = {"items": "templates/form_grid/stock_entry_grid.html"}


class StockEntry(StockController, SubcontractingInwardController):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.stock.doctype.landed_cost_taxes_and_charges.landed_cost_taxes_and_charges import (
			LandedCostTaxesandCharges,
		)
		from erpnext.stock.doctype.stock_entry_detail.stock_entry_detail import StockEntryDetail

		add_to_transit: DF.Check
		additional_costs: DF.Table[LandedCostTaxesandCharges]
		address_display: DF.TextEditor | None
		amended_from: DF.Link | None
		apply_putaway_rule: DF.Check
		asset_repair: DF.Link | None
		bom_no: DF.Link | None
		company: DF.Link
		cost_center: DF.Link | None
		credit_note: DF.Link | None
		delivery_note_no: DF.Link | None
		fg_completed_qty: DF.Float
		from_bom: DF.Check
		from_warehouse: DF.Link | None
		inspection_required: DF.Check
		is_additional_transfer_entry: DF.Check
		is_opening: DF.Literal["No", "Yes"]
		is_return: DF.Check
		items: DF.Table[StockEntryDetail]
		job_card: DF.Link | None
		letter_head: DF.Link | None
		naming_series: DF.Literal["MAT-STE-.YYYY.-"]
		outgoing_stock_entry: DF.Link | None
		per_transferred: DF.Percent
		pick_list: DF.Link | None
		posting_date: DF.Date | None
		posting_time: DF.Time | None
		process_loss_percentage: DF.Percent
		process_loss_qty: DF.Float
		project: DF.Link | None
		purchase_order: DF.Link | None
		purchase_receipt_no: DF.Link | None
		purpose: DF.Literal[
			"Material Issue",
			"Material Receipt",
			"Material Transfer",
			"Material Transfer for Manufacture",
			"Material Consumption for Manufacture",
			"Manufacture",
			"Repack",
			"Send to Subcontractor",
			"Disassemble",
			"Receive from Customer",
			"Return Raw Material to Customer",
			"Subcontracting Delivery",
			"Subcontracting Return",
		]
		remarks: DF.Text | None
		sales_invoice_no: DF.Link | None
		scan_barcode: DF.Data | None
		select_print_heading: DF.Link | None
		set_posting_time: DF.Check
		source_address_display: DF.TextEditor | None
		source_stock_entry: DF.Link | None
		source_warehouse_address: DF.Link | None
		stock_entry_type: DF.Link
		subcontracting_inward_order: DF.Link | None
		subcontracting_order: DF.Link | None
		supplier: DF.Link | None
		supplier_address: DF.Link | None
		supplier_name: DF.Data | None
		target_address_display: DF.TextEditor | None
		target_warehouse_address: DF.Link | None
		to_warehouse: DF.Link | None
		total_additional_costs: DF.Currency
		total_amount: DF.Currency
		total_incoming_value: DF.Currency
		total_outgoing_value: DF.Currency
		use_multi_level_bom: DF.Check
		value_difference: DF.Currency
		work_order: DF.Link | None
	# end: auto-generated types

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		if self.purchase_order:
			self.subcontract_data = frappe._dict(
				{
					"order_doctype": "Purchase Order",
					"order_field": "purchase_order",
					"rm_detail_field": "po_detail",
					"order_supplied_items_field": "Purchase Order Item Supplied",
				}
			)
		elif self.subcontracting_inward_order:
			self.subcontract_data = frappe._dict(
				{
					"order_doctype": "Subcontracting Inward Order",
					"order_field": "subcontracting_inward_order",
					"rm_detail_field": "scio_detail",
					"order_received_items_field": "Subcontracting Inward Order Received Item",
				}
			)
		else:
			self.subcontract_data = frappe._dict(
				{
					"order_doctype": "Subcontracting Order",
					"order_field": "subcontracting_order",
					"rm_detail_field": "sco_rm_detail",
					"order_supplied_items_field": "Subcontracting Order Supplied Item",
				}
			)

	def onload(self):
		self.update_items_from_bin_details()

	def before_print(self, settings=None):
		super().before_print(settings)
		self.update_items_from_bin_details()

	def update_items_from_bin_details(self):
		for item in self.get("items"):
			item.update(get_bin_details(item.item_code, item.s_warehouse or item.t_warehouse))

	def before_insert(self):
		if self.subcontracting_order and frappe.get_cached_value(
			"Subcontracting Order", self.subcontracting_order, "reserve_stock"
		):
			self.set_serial_batch_from_reserved_entry()

	def before_validate(self):
		from erpnext.stock.doctype.putaway_rule.putaway_rule import apply_putaway_rule

		apply_rule = self.apply_putaway_rule and (self.purpose in ["Material Transfer", "Material Receipt"])

		if self.get("items") and apply_rule:
			if items := apply_putaway_rule(
				self.doctype, self.get("items"), self.company, purpose=self.purpose
			):
				self.items = items

		if self.project:
			for item in self.items:
				if not item.project:
					item.project = self.project

	def validate(self):
		self.validate_duplicate_serial_and_batch_bundle("items")
		self.validate_posting_time()
		self.validate_item()
		self.validate_customer_provided_item()
		self.set_transfer_qty()
		self.validate_uom_is_integer("uom", "qty")
		self.validate_uom_is_integer("stock_uom", "transfer_qty")
		self.validate_warehouse()
		self.validate_warehouse_of_sabb()
		self.validate_handler()
		self.validate_source_stock_entry()
		self.validate_bom()
		self.set_process_loss_qty()
		self.validate_purchase_order()
		self.validate_company_in_accounting_dimension()

		if self.purpose in ("Manufacture", "Repack"):
			self.mark_finished_and_secondary_items()
			if not self.job_card:
				self.validate_finished_goods()
			else:
				self.validate_job_card_fg_item()

		self.validate_with_material_request()
		self.validate_batch()
		self.validate_inspection()
		self.validate_fg_completed_qty()
		self.validate_difference_account()
		self.set_job_card_data()
		self.validate_job_card_item()
		self.set_purpose_for_stock_entry()
		self.clean_serial_nos()
		self.validate_repack_entry()

		if not self.from_bom:
			self.fg_completed_qty = 0.0

		self.validate_serialized_batch()
		self.calculate_rate_and_amount()
		self.validate_putaway_capacity()
		self.validate_component_and_quantities()

		if self.get("purpose") != "Manufacture":
			# ignore other item wh difference and empty source/target wh
			# in Manufacture Entry
			self.reset_default_field_value("from_warehouse", "items", "s_warehouse")
			self.reset_default_field_value("to_warehouse", "items", "t_warehouse")

		self.validate_same_source_target_warehouse_during_material_transfer()

		self.validate_closed_subcontracting_order()
		self.validate_subcontract_order()
		self.validate_raw_materials_exists()

		super().validate_subcontracting_inward()

	def validate_handler(self):
		PURPOSE_HANDLER_MAP = {
			"Manufacture": ManufactureHandler,
			"Material Consumption for Manufacture": ManufactureHandler,
		}

		handler_class = PURPOSE_HANDLER_MAP.get(self.purpose)
		if handler_class:
			handler = handler_class(self)
			handler.validate()

	def before_submit(self):
		StockEntrySABB(self).make_serial_and_batch_bundle_for_outward()

	def validate_repack_entry(self):
		if self.purpose != "Repack":
			return

		fg_items = {row.item_code: row for row in self.items if row.is_finished_item}

		if len(fg_items) > 1 and not all(row.set_basic_rate_manually for row in fg_items.values()):
			frappe.throw(
				_(
					"When there are multiple finished goods ({0}) in a Repack stock entry, the basic rate for all finished goods must be set manually. To set rate manually, enable the checkbox 'Set Basic Rate Manually' in the respective finished good row."
				).format(", ".join(fg_items)),
				title=_("Set Basic Rate Manually"),
			)

	def validate_raw_materials_exists(self):
		if self.purpose not in ["Manufacture", "Repack", "Disassemble"]:
			return

		if frappe.db.get_single_value("Manufacturing Settings", "material_consumption"):
			return

		raw_materials = []
		for row in self.items:
			if row.s_warehouse:
				raw_materials.append(row.item_code)

		if not raw_materials:
			frappe.throw(
				_(
					"At least one raw material item must be present in the stock entry for the type {0}"
				).format(bold(self.purpose)),
				title=_("Raw Materials Missing"),
			)

	def on_submit(self):
		if self.purpose == "Disassemble":
			DisassembleHandler(self).process_on_submit()

		self.make_bundle_using_old_serial_batch_fields()
		self.update_work_order()
		self.update_disassembled_order()
		self.adjust_stock_reservation_entries_for_return()
		self.update_stock_reservation_entries()
		self.update_stock_ledger()
		self.make_stock_reserve_for_wip_and_fg()
		self.reserve_stock_for_subcontracting()

		self.update_subcontract_order_supplied_items()
		self.update_subcontracting_order_status()
		self.update_pick_list_status()

		self.make_gl_entries()

		self.repost_future_sle_and_gle()
		self.update_cost_in_project()
		self.update_transferred_qty()
		self.update_quality_inspection()

		if self.purpose == "Material Transfer" and self.add_to_transit:
			self.set_material_request_transfer_status("In Transit")
		if self.purpose == "Material Transfer" and self.outgoing_stock_entry:
			self.set_material_request_transfer_status("Completed")

		super().on_submit_subcontracting_inward()

	def on_cancel(self):
		self.delink_asset_repair_sabb()
		self.validate_closed_subcontracting_order()
		self.update_subcontract_order_supplied_items()
		self.update_subcontracting_order_status()
		self.cancel_stock_reserve_for_wip_and_fg()

		if self.work_order and self.purpose == "Material Consumption for Manufacture":
			self.validate_work_order_status()

		self.update_work_order()
		self.update_disassembled_order()
		self.cancel_stock_reservation_entries_for_inward()
		self.update_stock_ledger()

		self.ignore_linked_doctypes = (
			"GL Entry",
			"Stock Ledger Entry",
			"Repost Item Valuation",
			"Serial and Batch Bundle",
		)

		self.make_gl_entries_on_cancel()
		self.repost_future_sle_and_gle()
		self.update_cost_in_project()
		self.update_transferred_qty()
		self.update_quality_inspection()
		self.adjust_stock_reservation_entries_for_return()
		self.update_stock_reservation_entries()
		self.delete_auto_created_batches()
		self.delete_linked_stock_entry()

		if self.purpose == "Material Transfer" and self.add_to_transit:
			self.set_material_request_transfer_status("Not Started")
		if self.purpose == "Material Transfer" and self.outgoing_stock_entry:
			self.set_material_request_transfer_status("In Transit")

		super().on_cancel_subcontracting_inward()

	def on_update(self):
		super().on_update()
		self.set_serial_and_batch_bundle()

	def set_job_card_data(self):
		if self.job_card and not self.work_order:
			data = frappe.db.get_value(
				"Job Card", self.job_card, ["for_quantity", "work_order", "bom_no", "semi_fg_bom"], as_dict=1
			)
			self.fg_completed_qty = data.for_quantity
			self.work_order = data.work_order
			self.from_bom = 1
			self.bom_no = data.semi_fg_bom or data.bom_no

	def validate_job_card_fg_item(self):
		if not self.job_card:
			return

		job_card = frappe.db.get_value(
			"Job Card", self.job_card, ["finished_good", "manufactured_qty"], as_dict=1
		)

		for row in self.items:
			if row.is_finished_item and row.item_code != job_card.finished_good:
				frappe.throw(
					_("Row #{0}: Finished Good must be {1}").format(row.idx, job_card.fininshed_good)
				)

	def validate_job_card_item(self):
		if not self.job_card or self.purpose == "Manufacture":
			return

		if cint(frappe.db.get_single_value("Manufacturing Settings", "job_card_excess_transfer")):
			return

		for row in self.items:
			if row.job_card_item or not row.s_warehouse:
				continue

			msg = f"""Row #{row.idx}: The job card item reference
				is missing. Kindly create the stock entry
				from the job card. If you have added the row manually
				then you won't be able to add job card item reference."""

			frappe.throw(_(msg))

	def validate_work_order_status(self):
		pro_doc = frappe.get_doc("Work Order", self.work_order)
		if pro_doc.status == "Completed":
			frappe.throw(_("Cannot cancel transaction for Completed Work Order."))

	def delete_linked_stock_entry(self):
		if self.purpose == "Send to Warehouse":
			for d in frappe.get_all(
				"Stock Entry",
				filters={
					"docstatus": 0,
					"outgoing_stock_entry": self.name,
					"purpose": "Receive at Warehouse",
				},
			):
				frappe.delete_doc("Stock Entry", d.name)

	def delink_asset_repair_sabb(self):
		if not self.asset_repair:
			return

		for row in self.items:
			row.delink_asset_repair_sabb(self.asset_repair)

	def set_transfer_qty(self):
		self.validate_qty_is_not_zero()
		for item in self.get("items"):
			item.set_transfer_qty()

	def update_cost_in_project(self):
		if self.work_order and not frappe.db.get_value(
			"Work Order", self.work_order, "update_consumed_material_cost_in_project"
		):
			return

		projects = set(item.project for item in self.items if item.project)
		for project in projects:
			project_doc = frappe.get_doc("Project", project)
			project_doc.set_consumed_material_cost()
			project_doc.save()

	def validate_item(self):
		for item in self.get("items"):
			item_details = self.get_item_details(
				frappe._dict(
					{
						"item_code": item.item_code,
						"company": self.company,
						"project": self.project,
						"uom": item.uom,
						"s_warehouse": item.s_warehouse,
						"is_finished_item": item.is_finished_item,
					}
				),
				for_update=True,
			)

			item.validate_and_update_item_details(item_details, self.company, self.purpose)

	def validate_fg_completed_qty(self):
		if self.purpose != "Manufacture" or not self.from_bom:
			return

		fg_qty = defaultdict(float)
		for d in self.items:
			if d.is_finished_item:
				fg_qty[d.item_code] += flt(d.qty)

		if not fg_qty:
			return

		precision = frappe.get_precision("Stock Entry Detail", "qty")
		fg_item = next(iter(fg_qty.keys()))
		fg_item_qty = flt(fg_qty[fg_item], precision)
		fg_completed_qty = flt(self.fg_completed_qty, precision)

		for d in self.items:
			if not fg_qty.get(d.item_code):
				continue

			if (fg_completed_qty - fg_item_qty) > 0:
				self.process_loss_qty = fg_completed_qty - fg_item_qty

			if not self.process_loss_qty:
				continue

			if fg_completed_qty != (flt(fg_item_qty, precision) + flt(self.process_loss_qty, precision)):
				frappe.throw(
					_(
						"Since there is a process loss of {0} units for the finished good {1}, you should reduce the quantity by {0} units for the finished good {1} in the Items Table."
					).format(frappe.bold(self.process_loss_qty), frappe.bold(d.item_code))
				)

	def validate_difference_account(self):
		if not cint(erpnext.is_perpetual_inventory_enabled(self.company)):
			return

		for d in self.get("items"):
			d.validate_expense_account(self.is_opening, self.purpose)

	def validate_warehouse(self):
		"""perform various (sometimes conditional) validations on warehouse"""

		has_bom = any([d.bom_no for d in self.get("items")])

		for d in self.get("items"):
			d.remove_warehouse_if_not_required(self)
			d.validate_warehouse_for_purpose(self.purpose, has_bom, self.from_warehouse, self.to_warehouse)

	def validate_source_stock_entry(self):
		if not self.get("source_stock_entry"):
			return

		if self.work_order:
			source_wo = frappe.db.get_value("Stock Entry", self.source_stock_entry, "work_order")
			if source_wo and source_wo != self.work_order:
				frappe.throw(
					_(
						"Source Stock Entry {0} belongs to Work Order {1}, not {2}. Please use a manufacture entry from the same Work Order."
					).format(self.source_stock_entry, source_wo, self.work_order),
					title=_("Work Order Mismatch"),
				)

		from erpnext.manufacturing.doctype.work_order.work_order import get_disassembly_available_qty

		available_qty = get_disassembly_available_qty(self.source_stock_entry, self.name)

		if flt(self.fg_completed_qty) > available_qty:
			frappe.throw(
				_(
					"Cannot disassemble {0} qty against Stock Entry {1}. Only {2} qty available to disassemble."
				).format(
					self.fg_completed_qty,
					self.source_stock_entry,
					available_qty,
				),
				title=_("Excess Disassembly"),
			)

	def set_actual_qty(self):
		for d in self.get("items"):
			d.set_actual_qty(self.posting_date, self.posting_time)

	def validate_component_and_quantities(self):
		if self.purpose not in ["Manufacture", "Material Transfer for Manufacture"]:
			return

		if not frappe.db.get_single_value("Manufacturing Settings", "validate_components_quantities_per_bom"):
			return

		if not self.fg_completed_qty:
			return

		raw_materials = self.get_bom_raw_materials(self.fg_completed_qty)

		precision = frappe.get_precision("Stock Entry Detail", "qty")
		for item_code, details in raw_materials.items():
			item_code = item_code[0] if type(item_code) == tuple else item_code
			if matched_item := self.get_matched_items(item_code):
				if flt(details.get("qty"), precision) != flt(matched_item.qty, precision):
					frappe.throw(
						_(
							"For the item {0}, the consumed quantity should be {1} according to the BOM {2}."
						).format(
							frappe.bold(item_code),
							flt(details.get("qty")),
							get_link_to_form("BOM", self.bom_no),
						),
						title=_("Incorrect Component Quantity"),
					)
			else:
				frappe.throw(
					_("According to the BOM {0}, the Item '{1}' is missing in the stock entry.").format(
						get_link_to_form("BOM", self.bom_no), frappe.bold(item_code)
					),
					title=_("Missing Item"),
				)

	def validate_same_source_target_warehouse_during_material_transfer(self):
		"""
		Raises: frappe.ValidationError: If warehouses are same and no inventory dimensions differ
		"""

		if frappe.get_single_value("Stock Settings", "validate_material_transfer_warehouses"):
			from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions

			inventory_dimensions = get_inventory_dimensions()
			if self.purpose == "Material Transfer":
				for item in self.items:
					if cstr(item.s_warehouse) == cstr(item.t_warehouse):
						if not inventory_dimensions:
							frappe.throw(
								_(
									"Row #{0}: Source and Target Warehouse cannot be the same for Material Transfer"
								).format(item.idx),
								title=_("Invalid Source and Target Warehouse"),
							)
						else:
							difference_found = False
							for dimension in inventory_dimensions:
								fieldname = (
									dimension.source_fieldname
									if dimension.source_fieldname.startswith("to_")
									else f"to_{dimension.source_fieldname}"
								)
								if (
									item.get(dimension.source_fieldname)
									and item.get(fieldname)
									and item.get(dimension.source_fieldname) != item.get(fieldname)
								):
									difference_found = True
									break
							if not difference_found:
								frappe.throw(
									_(
										"Row #{0}: Source, Target Warehouse and Inventory Dimensions cannot be the exact same for Material Transfer"
									).format(item.idx),
									title=_("Invalid Source and Target Warehouse"),
								)

	def get_matched_items(self, item_code):
		items = [item for item in self.items if item.s_warehouse]
		for row in items or self.get_consumed_items():
			if row.item_code == item_code or row.original_item == item_code:
				return row

		return {}

	def get_consumed_items(self):
		"""Get all raw materials consumed through consumption entries"""
		parent = frappe.qb.DocType("Stock Entry")
		child = frappe.qb.DocType("Stock Entry Detail")

		query = (
			frappe.qb.from_(parent)
			.join(child)
			.on(parent.name == child.parent)
			.select(
				child.item_code,
				Sum(child.qty).as_("qty"),
				child.original_item,
			)
			.where(
				(parent.docstatus == 1)
				& (parent.purpose == "Material Consumption for Manufacture")
				& (parent.work_order == self.work_order)
			)
			.groupby(child.item_code, child.original_item)
		)

		return query.run(as_dict=True)

	@frappe.whitelist()
	def get_stock_and_rate(self):
		self.set_work_order_details()
		self.set_transfer_qty()
		self.set_actual_qty()
		self.calculate_rate_and_amount()

	def calculate_rate_and_amount(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
		self.set_basic_rate(reset_outgoing_rate, raise_error_if_no_rate)
		init_landed_taxes_and_totals(self)
		self.distribute_additional_costs()
		self.update_valuation_rate()
		self.set_total_incoming_outgoing_value()
		self.set_total_amount()

	def set_basic_rate(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
		"""
		Set rate for outgoing, secondary and finished items
		"""
		# Set rate for outgoing items
		outgoing_items_cost = self.set_rate_for_outgoing_items(reset_outgoing_rate, raise_error_if_no_rate)

		items = []
		# Set basic rate for incoming items
		for d in self.get("items"):
			if d.s_warehouse or d.set_basic_rate_manually:
				continue

			if d.allow_zero_valuation_rate and d.basic_rate and self.purpose != "Receive from Customer":
				d.basic_rate = 0.0
				items.append(d.item_code)
			elif d.is_finished_item:
				if self.purpose == "Manufacture":
					d.basic_rate = self.get_basic_rate_for_manufactured_item(
						d.transfer_qty, outgoing_items_cost
					)
				elif self.purpose == "Repack":
					d.basic_rate = self.get_basic_rate_for_repacked_items(d.transfer_qty, outgoing_items_cost)

				if self.bom_no:
					d.basic_rate *= frappe.get_value("BOM", self.bom_no, "cost_allocation_per") / 100
			elif d.type and d.bom_secondary_item:
				cost_allocation_per = frappe.get_value(
					"BOM Secondary Item", d.bom_secondary_item, "cost_allocation_per"
				)
				d.basic_rate = (outgoing_items_cost * (cost_allocation_per / 100)) / d.transfer_qty

			if not d.basic_rate and not d.allow_zero_valuation_rate:
				if self.is_new():
					raise_error_if_no_rate = False

				d.basic_rate = get_valuation_rate(
					d.item_code,
					d.t_warehouse,
					self.doctype,
					self.name,
					d.allow_zero_valuation_rate,
					currency=erpnext.get_company_currency(self.company),
					company=self.company,
					raise_error_if_no_rate=raise_error_if_no_rate,
					batch_no=d.batch_no,
					serial_and_batch_bundle=d.serial_and_batch_bundle,
				)

			# do not round off basic rate to avoid precision loss
			d.basic_rate = flt(d.basic_rate)
			d.basic_amount = flt(flt(d.transfer_qty) * flt(d.basic_rate), d.precision("basic_amount"))

		if items:
			message = ""

			if len(items) > 1:
				message = _(
					"Items rate has been updated to zero as Allow Zero Valuation Rate is checked for the following items: {0}"
				).format(", ".join(frappe.bold(item) for item in items))
			else:
				message = _(
					"Item rate has been updated to zero as Allow Zero Valuation Rate is checked for item {0}"
				).format(frappe.bold(items[0]))

			frappe.msgprint(message, alert=True)

	def set_rate_for_outgoing_items(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
		outgoing_items_cost = 0.0
		for d in self.get("items"):
			if d.s_warehouse:
				if reset_outgoing_rate:
					args = self.get_args_for_incoming_rate(d)
					rate = get_incoming_rate(args, raise_error_if_no_rate)
					if rate >= 0:
						d.basic_rate = rate

				d.basic_amount = flt(flt(d.transfer_qty) * flt(d.basic_rate), d.precision("basic_amount"))
				if not d.t_warehouse:
					outgoing_items_cost += flt(d.basic_amount)

		return outgoing_items_cost

	def get_args_for_incoming_rate(self, item):
		return frappe._dict(
			{
				"item_code": item.item_code,
				"warehouse": item.s_warehouse or item.t_warehouse,
				"posting_date": self.posting_date,
				"posting_time": self.posting_time,
				"qty": item.s_warehouse and -1 * flt(item.transfer_qty) or flt(item.transfer_qty),
				"voucher_type": self.doctype,
				"voucher_no": self.name,
				"company": self.company,
				"allow_zero_valuation": item.allow_zero_valuation_rate,
				"serial_and_batch_bundle": item.serial_and_batch_bundle,
				"voucher_detail_no": item.name,
				"batch_no": item.batch_no,
				"serial_no": item.serial_no,
			}
		)

	def get_basic_rate_for_repacked_items(self, finished_item_qty, outgoing_items_cost):
		finished_items = [d.item_code for d in self.get("items") if d.is_finished_item]
		if len(finished_items) == 1:
			return flt(outgoing_items_cost / finished_item_qty)
		else:
			unique_finished_items = set(finished_items)
			if len(unique_finished_items) == 1:
				total_fg_qty = sum([flt(d.transfer_qty) for d in self.items if d.is_finished_item])
				return flt(outgoing_items_cost / total_fg_qty)

	def get_basic_rate_for_manufactured_item(self, finished_item_qty, outgoing_items_cost=0) -> float:
		settings = frappe.get_single("Manufacturing Settings")
		scrap_items_cost = sum([flt(d.basic_amount) for d in self.get("items") if d.is_legacy_scrap_item])

		if settings.material_consumption:
			if settings.get_rm_cost_from_consumption_entry and self.work_order:
				# Validate only if Material Consumption Entry exists for the Work Order.
				if frappe.db.exists(
					"Stock Entry",
					{
						"docstatus": 1,
						"work_order": self.work_order,
						"purpose": "Material Consumption for Manufacture",
					},
				):
					for item in self.items:
						if not item.is_finished_item and not item.type and not item.is_legacy_scrap_item:
							label = frappe.get_meta(settings.doctype).get_label(
								"get_rm_cost_from_consumption_entry"
							)
							frappe.throw(
								_(
									"Row {0}: As {1} is enabled, raw materials cannot be added to {2} entry. Use {3} entry to consume raw materials."
								).format(
									item.idx,
									frappe.bold(label),
									frappe.bold(_("Manufacture")),
									frappe.bold(_("Material Consumption for Manufacture")),
								)
							)

					if frappe.db.exists(
						"Stock Entry",
						{
							"docstatus": 1,
							"work_order": self.work_order,
							"purpose": "Manufacture",
							"name": ("!=", self.name),
						},
					):
						frappe.throw(
							_("Only one {0} entry can be created against the Work Order {1}").format(
								frappe.bold(_("Manufacture")), frappe.bold(self.work_order)
							)
						)

					SE = frappe.qb.DocType("Stock Entry")
					SE_ITEM = frappe.qb.DocType("Stock Entry Detail")

					outgoing_items_cost = (
						frappe.qb.from_(SE)
						.left_join(SE_ITEM)
						.on(SE.name == SE_ITEM.parent)
						.select(Sum(SE_ITEM.valuation_rate * SE_ITEM.transfer_qty))
						.where(
							(SE.docstatus == 1)
							& (SE.work_order == self.work_order)
							& (SE.purpose == "Material Consumption for Manufacture")
						)
					).run()[0][0] or 0

			elif not outgoing_items_cost:
				bom_items = self.get_bom_raw_materials(finished_item_qty)
				outgoing_items_cost = sum([flt(row.qty) * flt(row.rate) for row in bom_items.values()])

		return flt((outgoing_items_cost - scrap_items_cost) / finished_item_qty)

	def distribute_additional_costs(self):
		# If no incoming items, set additional costs blank
		if not any(d.item_code for d in self.items if d.t_warehouse):
			self.additional_costs = []

		self.total_additional_costs = sum(flt(t.base_amount) for t in self.get("additional_costs"))

		if self.purpose in ("Repack", "Manufacture"):
			incoming_items_cost = sum(flt(t.basic_amount) for t in self.get("items") if t.is_finished_item)
		else:
			incoming_items_cost = sum(flt(t.basic_amount) for t in self.get("items") if t.t_warehouse)

		if not incoming_items_cost:
			return

		for d in self.get("items"):
			if self.purpose in ("Repack", "Manufacture") and not d.is_finished_item:
				d.additional_cost = 0
				continue
			elif not d.t_warehouse:
				d.additional_cost = 0
				continue
			d.additional_cost = (flt(d.basic_amount) / incoming_items_cost) * self.total_additional_costs

	def update_valuation_rate(self, reset_outgoing_rate=True):
		for d in self.get("items"):
			if not reset_outgoing_rate and d.s_warehouse:
				continue

			if d.transfer_qty:
				d.amount = flt(
					flt(flt(d.basic_amount) + flt(d.additional_cost) + flt(d.landed_cost_voucher_amount)),
					d.precision("amount"),
				)
				# Do not round off valuation rate to avoid precision loss
				d.valuation_rate = flt(d.basic_rate) + (
					flt(flt(d.additional_cost) + flt(d.landed_cost_voucher_amount)) / flt(d.transfer_qty)
				)

	def set_total_incoming_outgoing_value(self):
		self.total_incoming_value = self.total_outgoing_value = 0.0
		for d in self.get("items"):
			if d.t_warehouse:
				self.total_incoming_value += flt(d.amount)
			if d.s_warehouse:
				self.total_outgoing_value += flt(d.amount)

		self.value_difference = self.total_incoming_value - self.total_outgoing_value

	def set_total_amount(self):
		self.total_amount = None
		if self.purpose not in ["Manufacture", "Repack"]:
			self.total_amount = sum([flt(item.amount) for item in self.get("items")])

	def set_stock_entry_type(self):
		if self.purpose:
			self.stock_entry_type = frappe.get_cached_value(
				"Stock Entry Type", {"purpose": self.purpose, "is_standard": 1}, "name"
			)

	def set_purpose_for_stock_entry(self):
		if self.stock_entry_type and not self.purpose:
			self.purpose = frappe.get_cached_value("Stock Entry Type", self.stock_entry_type, "purpose")

	def validate_subcontract_order(self):
		"""Throw exception if more raw material is transferred against Subcontract Order than in
		the raw materials supplied table"""
		backflush_raw_materials_based_on = frappe.db.get_single_value(
			"Buying Settings", "backflush_raw_materials_of_subcontract_based_on"
		)

		if not (self.purpose == "Send to Subcontractor" and self.get(self.subcontract_data.order_field)):
			return

		if backflush_raw_materials_based_on == "BOM":
			subcontract_order = frappe.get_doc(
				self.subcontract_data.order_doctype, self.get(self.subcontract_data.order_field)
			)
			for se_item in self.items:
				se_item.validate_subcontracting_order_for_bom(subcontract_order, self)

		elif backflush_raw_materials_based_on == "Material Transferred for Subcontract":
			for row in self.items:
				row.validate_subcontracting_order_for_transfer(self)

	def validate_bom(self):
		for d in self.get("items"):
			if d.bom_no and d.is_finished_item:
				item_code = d.original_item or d.item_code
				validate_bom_no(item_code, d.bom_no)

	def validate_purchase_order(self):
		if self.purpose == "Send to Subcontractor" and self.get("purchase_order"):
			is_old_subcontracting_flow = frappe.db.get_value(
				"Purchase Order", self.purchase_order, "is_old_subcontracting_flow"
			)

			if not is_old_subcontracting_flow:
				frappe.throw(
					_("Please select Subcontracting Order instead of Purchase Order {0}").format(
						self.purchase_order
					)
				)

	def validate_closed_subcontracting_order(self):
		order = self.get("subcontracting_order") or self.get("subcontracting_inward_order")
		if order:
			check_on_hold_or_closed_status(
				"Subcontracting Order" if self.get("subcontracting_order") else "Subcontracting Inward Order",
				order,
			)

	def mark_finished_and_secondary_items(self):
		if self.purpose != "Repack" and any(
			[d.item_code for d in self.items if (d.is_finished_item and d.t_warehouse)]
		):
			return

		finished_item = self.get_finished_item()

		if not finished_item and self.purpose == "Manufacture":
			# In case of independent Manufacture entry, don't auto set
			# user must decide and set
			return

		for d in self.items:
			if d.t_warehouse and not d.s_warehouse:
				if self.purpose == "Repack" or d.item_code == finished_item:
					d.is_finished_item = 1
			else:
				d.is_finished_item = 0
				d.type = ""

	def get_finished_item(self):
		finished_item = None
		if self.work_order:
			finished_item = frappe.db.get_value("Work Order", self.work_order, "production_item")
		elif self.bom_no:
			finished_item = frappe.db.get_value("BOM", self.bom_no, "item")

		return finished_item

	def validate_finished_goods(self):
		"""
		1. Check if FG exists (mfg, repack)
		2. Check if Multiple FG Items are present (mfg)
		3. Check FG Item and Qty against WO if present (mfg)
		"""
		production_item, wo_qty, finished_items = None, 0, []
		if self.work_order:
			wo_details = frappe.db.get_value("Work Order", self.work_order, ["production_item", "qty"])
			if wo_details:
				production_item, wo_qty = wo_details

		for d in self.get("items"):
			if d.is_finished_item:
				if not self.work_order:
					# Independent MFG Entry/ Repack Entry, no WO to match against
					finished_items.append(d.item_code)
					continue

				if d.item_code != production_item:
					frappe.throw(
						_("Finished Item {0} does not match with Work Order {1}").format(
							d.item_code, self.work_order
						)
					)

				finished_items.append(d.item_code)

		if not finished_items:
			frappe.throw(
				msg=_("There must be atleast 1 Finished Good in this Stock Entry").format(self.name),
				title=_("Missing Finished Good"),
				exc=FinishedGoodError,
			)

		if self.purpose == "Manufacture":
			if len(set(finished_items)) > 1:
				frappe.throw(
					msg=_("Multiple items cannot be marked as finished item"),
					title=_("Note"),
					exc=FinishedGoodError,
				)

			allowance_percentage = flt(
				frappe.db.get_single_value(
					"Manufacturing Settings", "overproduction_percentage_for_work_order"
				)
			)
			allowed_qty = wo_qty + ((allowance_percentage / 100) * wo_qty)

			# No work order could mean independent Manufacture entry, if so skip validation
			if self.work_order and self.fg_completed_qty > allowed_qty:
				frappe.throw(
					_("For quantity {0} should not be greater than allowed quantity {1}").format(
						flt(self.fg_completed_qty), allowed_qty
					)
				)

	def update_stock_ledger(self, allow_negative_stock=False, via_landed_cost_voucher=False):
		sl_entries = []
		finished_item_row = self.get_finished_item_row()

		# make sl entries for source warehouse first
		self.get_sle_for_source_warehouse(sl_entries, finished_item_row)

		# SLE for target warehouse
		self.get_sle_for_target_warehouse(sl_entries, finished_item_row)

		# reverse sl entries if cancel
		if self.docstatus == 2:
			sl_entries.reverse()

		self.make_sl_entries(
			sl_entries,
			allow_negative_stock=allow_negative_stock,
			via_landed_cost_voucher=via_landed_cost_voucher,
		)

	def get_finished_item_row(self):
		finished_item_row = None
		if self.purpose in ("Manufacture", "Repack"):
			for d in self.get("items"):
				if d.is_finished_item:
					finished_item_row = d

		return finished_item_row

	def validate_serial_batch_bundle_type(self, serial_and_batch_bundle):
		if (
			frappe.db.get_value("Serial and Batch Bundle", serial_and_batch_bundle, "type_of_transaction")
			!= "Outward"
		):
			frappe.throw(
				_(
					"The Serial and Batch Bundle {0} is not valid for this transaction. The 'Type of Transaction' should be 'Outward' instead of 'Inward' in Serial and Batch Bundle {0}"
				).format(get_link_to_form("Serial and Batch Bundle", serial_and_batch_bundle)),
				title=_("Invalid Serial and Batch Bundle"),
			)

	def get_sle_for_source_warehouse(self, sl_entries, finished_item_row):
		for d in self.get("items"):
			if cstr(d.s_warehouse):
				if d.serial_and_batch_bundle and self.docstatus == 1:
					self.validate_serial_batch_bundle_type(d.serial_and_batch_bundle)

				sle = self.get_sl_entries(
					d,
					{
						"warehouse": cstr(d.s_warehouse),
						"actual_qty": -flt(d.transfer_qty),
						"incoming_rate": 0,
					},
				)
				if cstr(d.t_warehouse):
					sle.dependant_sle_voucher_detail_no = d.name
				elif finished_item_row and (
					finished_item_row.item_code != d.item_code
					or finished_item_row.t_warehouse != d.s_warehouse
				):
					sle.dependant_sle_voucher_detail_no = finished_item_row.name

				if sle.serial_and_batch_bundle and self.docstatus == 2:
					bundle_id = frappe.get_cached_value(
						"Serial and Batch Bundle",
						{
							"voucher_detail_no": d.name,
							"voucher_no": self.name,
							"is_cancelled": 0,
							"type_of_transaction": "Outward",
						},
						"name",
					)

					if bundle_id:
						sle.serial_and_batch_bundle = bundle_id

				sl_entries.append(sle)

	def make_serial_and_batch_bundle_for_transfer(self):
		ids = frappe._dict(
			frappe.get_all(
				"Stock Entry Detail",
				fields=["name", "serial_and_batch_bundle"],
				filters={"parent": self.outgoing_stock_entry, "serial_and_batch_bundle": ("is", "set")},
				as_list=1,
			)
		)

		if not ids:
			return

		for d in self.get("items"):
			serial_and_batch_bundle = ids.get(d.ste_detail)
			if not serial_and_batch_bundle:
				continue

			d.serial_and_batch_bundle = self.make_package_for_transfer(
				serial_and_batch_bundle, d.s_warehouse, "Outward", do_not_submit=True
			)

	def get_sle_for_target_warehouse(self, sl_entries, finished_item_row):
		for d in self.get("items"):
			if cstr(d.t_warehouse):
				sle = self.get_sl_entries(
					d,
					{
						"warehouse": cstr(d.t_warehouse),
						"actual_qty": flt(d.transfer_qty),
						"incoming_rate": flt(d.valuation_rate),
					},
				)

				if cstr(d.s_warehouse) or (finished_item_row and d.name == finished_item_row.name):
					sle.recalculate_rate = 1

				allowed_types = [
					"Material Transfer",
					"Send to Subcontractor",
					"Material Transfer for Manufacture",
				]

				if self.purpose in allowed_types and d.serial_and_batch_bundle and self.docstatus == 1:
					sle.serial_and_batch_bundle = self.make_package_for_transfer(
						d.serial_and_batch_bundle, d.t_warehouse
					)

				if sle.serial_and_batch_bundle and self.docstatus == 2:
					bundle_id = frappe.get_cached_value(
						"Serial and Batch Bundle",
						{
							"voucher_detail_no": d.name,
							"voucher_no": self.name,
							"is_cancelled": 0,
							"type_of_transaction": "Inward",
						},
						"name",
					)

					if sle.serial_and_batch_bundle != bundle_id:
						sle.serial_and_batch_bundle = bundle_id

				sl_entries.append(sle)

	def get_gl_entries(self, inventory_account_map):
		gl_entries = super().get_gl_entries(inventory_account_map)

		if self.purpose in ("Repack", "Manufacture"):
			total_basic_amount = sum(flt(t.basic_amount) for t in self.get("items") if t.is_finished_item)
		else:
			total_basic_amount = sum(flt(t.basic_amount) for t in self.get("items") if t.t_warehouse)

		divide_based_on = total_basic_amount

		if self.get("additional_costs") and not total_basic_amount:
			# if total_basic_amount is 0, distribute additional charges based on qty
			divide_based_on = sum(item.qty for item in list(self.get("items")))

		item_account_wise_additional_cost = {}

		for t in self.get("additional_costs"):
			for d in self.get("items"):
				if self.purpose in ("Repack", "Manufacture") and not d.is_finished_item:
					continue
				elif not d.t_warehouse:
					continue

				item_account_wise_additional_cost.setdefault((d.item_code, d.name), {})
				item_account_wise_additional_cost[(d.item_code, d.name)].setdefault(
					t.expense_account, {"amount": 0.0, "base_amount": 0.0}
				)

				multiply_based_on = d.basic_amount if total_basic_amount else d.qty

				item_account_wise_additional_cost[(d.item_code, d.name)][t.expense_account]["amount"] += (
					flt(t.amount * multiply_based_on) / divide_based_on
				)

				item_account_wise_additional_cost[(d.item_code, d.name)][t.expense_account][
					"base_amount"
				] += flt(t.base_amount * multiply_based_on) / divide_based_on

		if item_account_wise_additional_cost:
			for d in self.get("items"):
				for account, amount in item_account_wise_additional_cost.get(
					(d.item_code, d.name), {}
				).items():
					if not amount:
						continue

					gl_entries.append(
						self.get_gl_dict(
							{
								"account": account,
								"against": d.expense_account,
								"cost_center": d.cost_center,
								"remarks": self.get("remarks") or _("Accounting Entry for Stock"),
								"credit_in_account_currency": flt(amount["amount"]),
								"credit": flt(amount["base_amount"]),
							},
							item=d,
						)
					)

					gl_entries.append(
						self.get_gl_dict(
							{
								"account": d.expense_account,
								"against": account,
								"cost_center": d.cost_center,
								"remarks": self.get("remarks") or _("Accounting Entry for Stock"),
								"credit": -1
								* amount[
									"base_amount"
								],  # put it as negative credit instead of debit purposefully
							},
							item=d,
						)
					)

		self.set_gl_entries_for_landed_cost_voucher(gl_entries, inventory_account_map)

		return process_gl_map(gl_entries, from_repost=frappe.flags.through_repost_item_valuation)

	def set_gl_entries_for_landed_cost_voucher(self, gl_entries, inventory_account_map):
		landed_cost_entries = self.get_item_account_wise_lcv_entries()
		if not landed_cost_entries:
			return

		for item in self.get("items"):
			if item.s_warehouse:
				continue

			if (item.item_code, item.name) in landed_cost_entries:
				for account, amount in landed_cost_entries[(item.item_code, item.name)].items():
					account_currency = get_account_currency(account)
					credit_amount = (
						flt(amount["base_amount"])
						if (amount["base_amount"] or account_currency != self.company_currency)
						else flt(amount["amount"])
					)

					_inv_dict = self.get_inventory_account_dict(item, inventory_account_map, "t_warehouse")
					gl_entries.append(
						self.get_gl_dict(
							{
								"account": account,
								"against": _inv_dict["account"],
								"cost_center": item.cost_center,
								"debit": 0.0,
								"credit": credit_amount,
								"remarks": _("Accounting Entry for LCV in Stock Entry {0}").format(self.name),
								"credit_in_account_currency": flt(amount["amount"]),
								"account_currency": account_currency,
								"project": item.project,
							},
							item=item,
						)
					)

					account_currency = get_account_currency(item.expense_account)

					# credit amount in negative to knock off the debit entry
					gl_entries.append(
						self.get_gl_dict(
							{
								"account": item.expense_account,
								"against": _inv_dict["account"],
								"cost_center": item.cost_center,
								"debit": 0.0,
								"credit": credit_amount * -1,
								"remarks": _("Accounting Entry for LCV in Stock Entry {0}").format(self.name),
								"debit_in_account_currency": flt(amount["amount"]),
								"account_currency": account_currency,
								"project": item.project,
							},
							item=item,
						)
					)

	def update_work_order(self):
		ManufactureHandler(self).update_job_card_and_work_order()

	@property
	def pro_doc(self):
		if not getattr(self, "_wo_doc", None):
			if self.work_order:
				self._wo_doc = frappe.get_doc("Work Order", self.work_order)
		return getattr(self, "_wo_doc", None)

	def update_disassembled_order(self):
		if not self.work_order:
			return
		if self.purpose == "Disassemble" and self.fg_completed_qty:
			pro_doc = frappe.get_doc("Work Order", self.work_order)
			pro_doc.run_method("update_disassembled_qty", self.fg_completed_qty, self._action == "cancel")

	def make_stock_reserve_for_wip_and_fg(self):
		if self.is_stock_reserve_for_work_order():
			pro_doc = frappe.get_doc("Work Order", self.work_order)
			if (
				self.purpose == "Manufacture"
				and not pro_doc.sales_order
				and not self.job_card
				and not pro_doc.production_plan_sub_assembly_item
				and not pro_doc.subcontracting_inward_order
			):
				return

			pro_doc.set_reserved_qty_for_wip_and_fg(self)

	def reserve_stock_for_subcontracting(self):
		if self.purpose == "Send to Subcontractor" and frappe.get_value(
			"Subcontracting Order", self.subcontracting_order, "reserve_stock"
		):
			items = {}
			for item in self.items:
				if item.sco_rm_detail in items:
					items[item.sco_rm_detail].qty_to_reserve += item.transfer_qty
					items[item.sco_rm_detail].serial_and_batch_bundles.append(item.serial_and_batch_bundle)
				else:
					items[item.sco_rm_detail] = frappe._dict(
						{
							"name": item.sco_rm_detail,
							"qty_to_reserve": item.transfer_qty,
							"warehouse": item.t_warehouse,
							"reference_voucher_detail_no": item.name,
							"serial_and_batch_bundles": [item.serial_and_batch_bundle],
						}
					)

			frappe.get_doc("Subcontracting Order", self.subcontracting_order).reserve_raw_materials(
				items=items.values(), stock_entry=self.name
			)

	def cancel_stock_reserve_for_wip_and_fg(self):
		if self.is_stock_reserve_for_work_order():
			pro_doc = frappe.get_doc("Work Order", self.work_order)
			if (
				self.purpose == "Manufacture"
				and not pro_doc.sales_order
				and not pro_doc.production_plan_sub_assembly_item
			):
				return

			pro_doc.cancel_reserved_qty_for_wip_and_fg(self)

	def is_stock_reserve_for_work_order(self):
		if (
			self.work_order
			and self.purpose in ["Material Transfer for Manufacture", "Manufacture"]
			and frappe.get_cached_value("Work Order", self.work_order, "reserve_stock")
		):
			return True

		return False

	@frappe.whitelist()
	def get_item_details(self, args: ItemDetailsCtx | None = None, for_update: bool = False):
		item = frappe.qb.DocType("Item")
		item_default = frappe.qb.DocType("Item Default")

		query = (
			frappe.qb.from_(item)
			.left_join(item_default)
			.on((item.name == item_default.parent) & (item_default.company == self.company))
			.select(
				item.name,
				item.stock_uom,
				item.description,
				item.image,
				item.is_stock_item,
				item.item_name,
				item.item_group,
				item.has_batch_no,
				item.sample_quantity,
				item.has_serial_no,
				item.allow_alternative_item,
				item_default.expense_account,
				item_default.buying_cost_center,
			)
			.where(
				(item.name == args.get("item_code"))
				& (item.disabled == 0)
				& (
					(item.end_of_life.isnull())
					| (item.end_of_life < "1900-01-01")
					| (item.end_of_life > nowdate())
				)
			)
		)
		item = query.run(as_dict=True)

		if not item:
			frappe.throw(
				_("Item {0} is not active or end of life has been reached").format(args.get("item_code"))
			)

		item = item[0]
		item_group_defaults = get_item_group_defaults(item.name, self.company)
		brand_defaults = get_brand_defaults(item.name, self.company)

		ret = frappe._dict(
			{
				"uom": item.stock_uom,
				"stock_uom": item.stock_uom,
				"description": item.description,
				"image": item.image,
				"item_name": item.item_name,
				"cost_center": get_default_cost_center(
					args, item, item_group_defaults, brand_defaults, self.company
				),
				"qty": args.get("qty"),
				"transfer_qty": args.get("qty"),
				"conversion_factor": 1,
				"actual_qty": 0,
				"basic_rate": 0,
				"has_serial_no": item.has_serial_no,
				"has_batch_no": item.has_batch_no,
				"sample_quantity": item.sample_quantity,
				"expense_account": item.expense_account or item_group_defaults.get("expense_account"),
				"is_stock_item": item.is_stock_item,
			}
		)

		if self.purpose == "Send to Subcontractor":
			ret["allow_alternative_item"] = item.allow_alternative_item

		# update uom
		if args.get("uom") and for_update:
			ret.update(get_uom_details(args.get("item_code"), args.get("uom"), args.get("qty")))

		if self.purpose == "Material Issue":
			ret["expense_account"] = item.get("expense_account") or item_group_defaults.get("expense_account")

		if not ret.get("expense_account"):
			ret["expense_account"] = frappe.get_cached_value(
				"Company", self.company, "stock_adjustment_account"
			)

		for company_field, field in {
			"stock_adjustment_account": "expense_account",
			"cost_center": "cost_center",
		}.items():
			if not ret.get(field):
				ret[field] = frappe.get_cached_value("Company", self.company, company_field)

		args["posting_date"] = self.posting_date
		args["posting_time"] = self.posting_time

		stock_and_rate = get_warehouse_details(args) if args.get("warehouse") else {}
		ret.update(stock_and_rate)

		if (
			self.purpose == "Send to Subcontractor"
			and self.get(self.subcontract_data.order_field)
			and args.get("item_code")
		):
			subcontract_items = frappe.get_all(
				self.subcontract_data.order_supplied_items_field,
				{
					"parent": self.get(self.subcontract_data.order_field),
					"rm_item_code": args.get("item_code"),
				},
				"main_item_code",
			)

			if subcontract_items and len(subcontract_items) == 1:
				ret["subcontracted_item"] = subcontract_items[0].main_item_code

		barcode_data = get_barcode_data(item_code=item.name)
		if barcode_data and len(barcode_data.get(item.name)) == 1:
			ret["barcode"] = barcode_data.get(item.name)[0]

		return ret

	@frappe.whitelist()
	def set_items_for_stock_in(self):
		self.items = []

		if self.outgoing_stock_entry and self.purpose == "Material Transfer":
			doc = frappe.get_doc("Stock Entry", self.outgoing_stock_entry)

			if doc.per_transferred == 100:
				frappe.throw(_("Goods are already received against the outward entry {0}").format(doc.name))

			for d in doc.items:
				self.append(
					"items",
					{
						"s_warehouse": d.t_warehouse,
						"item_code": d.item_code,
						"qty": d.qty,
						"uom": d.uom,
						"against_stock_entry": d.parent,
						"ste_detail": d.name,
						"stock_uom": d.stock_uom,
						"conversion_factor": d.conversion_factor,
					},
				)

	@frappe.whitelist()
	def get_items(self):
		PURPOSE_HANDLER_MAP = {
			"Manufacture": ManufactureHandler,
			"Material Transfer for Manufacture": MaterialTransferHandler,
			"Disassemble": DisassembleHandler,
			"Material Consumption for Manufacture": ManufactureHandler,
		}

		handler = None
		handler_class = PURPOSE_HANDLER_MAP.get(self.purpose)
		if handler_class:
			handler = handler_class(self)
		elif self.bom_no:
			handler = ManufactureHandler(self)

		self.set("items", [])
		if handler:
			handler.set_items()

		self.set_serial_batch_from_reserved_entry()
		self.set_actual_qty()
		self.validate_customer_provided_item()
		self.calculate_rate_and_amount(raise_error_if_no_rate=False)

	def set_serial_batch_from_reserved_entry(self):
		StockEntrySABB(self).set_serial_batch_based_on_reservation()

	def get_backflush_based_on(self):
		from erpnext.manufacturing.doctype.bom.bom import get_backflush_based_on

		return get_backflush_based_on(self.bom_no)

	def set_process_loss_qty(self):
		if self.purpose not in ("Manufacture", "Repack"):
			return

		precision = self.precision("process_loss_qty")
		if self.work_order:
			data = frappe.get_all(
				"Work Order Operation",
				filters={"parent": self.work_order},
				fields=[{"MAX": "process_loss_qty", "as": "process_loss_qty"}],
			)

			if data and data[0].process_loss_qty:
				process_loss_qty = data[0].process_loss_qty
				if flt(self.process_loss_qty, precision) != flt(process_loss_qty, precision):
					self.process_loss_qty = flt(process_loss_qty, precision)

					frappe.msgprint(
						_("The Process Loss Qty has reset as per job cards Process Loss Qty"), alert=True
					)

		if not self.process_loss_percentage and not self.process_loss_qty:
			self.process_loss_percentage = frappe.get_cached_value(
				"BOM", self.bom_no, "process_loss_percentage"
			)

		if self.process_loss_percentage and not self.process_loss_qty:
			self.process_loss_qty = flt(
				(flt(self.fg_completed_qty) * flt(self.process_loss_percentage)) / 100
			)
		elif self.process_loss_qty and not self.process_loss_percentage:
			self.process_loss_percentage = flt(
				(flt(self.process_loss_qty) / flt(self.fg_completed_qty)) * 100
			)

	def set_work_order_details(self):
		if self.work_order:
			# common validations
			if self.pro_doc and not self.pro_doc.track_semi_finished_goods:
				self.bom_no = self.pro_doc.bom_no
			else:
				# invalid work order
				self.work_order = None

	def get_bom_raw_materials(self, qty):
		from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict

		# item dict = { item_code: {qty, description, stock_uom} }
		item_dict = get_bom_items_as_dict(
			self.bom_no,
			self.company,
			qty=qty,
			fetch_exploded=self.use_multi_level_bom,
			fetch_qty_in_stock_uom=False,
		)

		used_alternative_items = get_used_alternative_items(
			subcontract_order_field=self.subcontract_data.order_field, work_order=self.work_order
		)
		for item in item_dict.values():
			# if source warehouse presents in BOM set from_warehouse as bom source_warehouse
			if item["allow_alternative_item"]:
				item["allow_alternative_item"] = frappe.db.get_value(
					"Work Order", self.work_order, "allow_alternative_item"
				)

			skip_transfer, from_wip_warehouse = (
				frappe.get_value("Work Order", self.work_order, ["skip_transfer", "from_wip_warehouse"])
				if self.work_order
				else [None, None]
			)

			item.from_warehouse = (
				frappe.get_value(
					"Work Order Item",
					{"parent": self.work_order, "item_code": item.item_code},
					"source_warehouse",
				)
				if skip_transfer and not from_wip_warehouse
				else self.from_warehouse or item.source_warehouse or item.default_warehouse
			)
			if item.item_code in used_alternative_items:
				alternative_item_data = used_alternative_items.get(item.item_code)
				item.item_code = alternative_item_data.item_code
				item.item_name = alternative_item_data.item_name
				item.stock_uom = alternative_item_data.stock_uom
				item.uom = alternative_item_data.uom
				item.conversion_factor = alternative_item_data.conversion_factor
				item.description = alternative_item_data.description

		return item_dict

	def add_to_stock_entry_detail(self, item_dict, bom_no=None):
		_handler = BaseManufacturingHandler(self)
		_handler.add_to_stock_entry_detail(item_dict, bom_no=bom_no)

	def validate_with_material_request(self):
		for item in self.get("items"):
			item.validate_material_request(self.purpose, self.outgoing_stock_entry)

	def validate_batch(self):
		if self.purpose in [
			"Material Transfer for Manufacture",
			"Manufacture",
			"Repack",
			"Send to Subcontractor",
		]:
			for item in self.get("items"):
				item.validate_batch()

	def update_subcontract_order_supplied_items(self):
		if self.get(self.subcontract_data.order_field) and (
			self.purpose in ["Send to Subcontractor", "Material Transfer"] or self.is_return
		):
			# Get Subcontract Order Supplied Items Details
			order_supplied_items = frappe.db.get_all(
				self.subcontract_data.order_supplied_items_field,
				filters={"parent": self.get(self.subcontract_data.order_field)},
				fields=["name", "rm_item_code", "reserve_warehouse"],
			)

			# Get Items Supplied in Stock Entries against Subcontract Order
			supplied_items = get_supplied_items(
				self.get(self.subcontract_data.order_field),
				self.subcontract_data.rm_detail_field,
				self.subcontract_data.order_field,
			)

			for row in order_supplied_items:
				key, item = row.name, {}
				if not supplied_items.get(key):
					# no stock transferred against Subcontract Order Supplied Items row
					item = {"supplied_qty": 0, "returned_qty": 0, "total_supplied_qty": 0}
				else:
					item = supplied_items.get(key)

				frappe.db.set_value(self.subcontract_data.order_supplied_items_field, row.name, item)

			# RM Item-Reserve Warehouse Dict
			item_wh = {x.get("rm_item_code"): x.get("reserve_warehouse") for x in order_supplied_items}

			for d in self.get("items"):
				# Update reserved sub contracted quantity in bin based on Supplied Item Details and
				item_code = d.get("original_item") or d.get("item_code")
				reserve_warehouse = item_wh.get(item_code)
				if not (reserve_warehouse and item_code):
					continue
				stock_bin = get_bin(item_code, reserve_warehouse)
				stock_bin.update_reserved_qty_for_sub_contracting()

	def update_transferred_qty(self):
		if self.purpose == "Material Transfer" and self.outgoing_stock_entry:
			stock_entries = {}
			stock_entries_child_list = []
			for d in self.items:
				if not (d.against_stock_entry and d.ste_detail):
					continue

				stock_entries_child_list.append(d.ste_detail)
				transferred_qty = frappe.get_all(
					"Stock Entry Detail",
					fields=[{"SUM": "transfer_qty", "as": "qty"}],
					filters={
						"against_stock_entry": d.against_stock_entry,
						"ste_detail": d.ste_detail,
						"docstatus": 1,
					},
				)

				if d.docstatus == 1:
					transfer_qty = frappe.get_value("Stock Entry Detail", d.ste_detail, "transfer_qty")

					if transferred_qty and transferred_qty[0]:
						if transferred_qty[0].qty > transfer_qty:
							frappe.throw(
								_(
									"Row {0}: Transferred quantity cannot be greater than the requested quantity."
								).format(d.idx)
							)

				stock_entries[(d.against_stock_entry, d.ste_detail)] = (
					transferred_qty[0].qty if transferred_qty and transferred_qty[0] else 0.0
				) or 0.0

			if not stock_entries:
				return None

			cond = ""
			for data, transferred_qty in stock_entries.items():
				cond += """ WHEN (parent = {} and name = {}) THEN {}
					""".format(
					frappe.db.escape(data[0]),
					frappe.db.escape(data[1]),
					transferred_qty,
				)

			if stock_entries_child_list:
				frappe.db.sql(
					""" UPDATE `tabStock Entry Detail`
					SET
						transferred_qty = CASE {cond} END
					WHERE
						name in ({ste_details}) """.format(
						cond=cond, ste_details=",".join(["%s"] * len(stock_entries_child_list))
					),
					tuple(stock_entries_child_list),
				)

			args = {
				"source_dt": "Stock Entry Detail",
				"target_field": "transferred_qty",
				"target_ref_field": "qty",
				"target_dt": "Stock Entry Detail",
				"join_field": "ste_detail",
				"target_parent_dt": "Stock Entry",
				"target_parent_field": "per_transferred",
				"source_field": "qty",
				"percent_join_field": "against_stock_entry",
			}

			self._update_percent_field_in_targets(args, update_modified=True)

	def update_quality_inspection(self):
		if self.inspection_required:
			reference_type = reference_name = ""
			if self.docstatus == 1:
				reference_name = self.name
				reference_type = "Stock Entry"

			for d in self.items:
				if d.quality_inspection:
					frappe.db.set_value(
						"Quality Inspection",
						d.quality_inspection,
						{"reference_type": reference_type, "reference_name": reference_name},
					)

	def set_material_request_transfer_status(self, status):
		from erpnext.stock.doctype.stock_entry_detail.stock_entry_detail import get_transferred_qty

		material_requests = []
		if self.outgoing_stock_entry:
			parent_se = frappe.get_value("Stock Entry", self.outgoing_stock_entry, "add_to_transit")

		for item in self.items:
			material_request = item.get("material_request")
			if self.purpose == "Material Transfer" and material_request not in material_requests:
				if self.outgoing_stock_entry and parent_se:
					material_request = frappe.get_value(
						"Stock Entry Detail", item.ste_detail, "material_request"
					)

			if material_request and material_request not in material_requests:
				material_requests.append(material_request)
				if status == "Completed":
					qty = get_transferred_qty(material_request)
					if qty.get("transfer_qty") > qty.get("transferred_qty"):
						status = "In Transit"

				frappe.db.set_value("Material Request", material_request, "transfer_status", status)

	def update_subcontracting_order_status(self):
		if self.subcontracting_order and self.purpose in ["Send to Subcontractor", "Material Transfer"]:
			from erpnext.subcontracting.doctype.subcontracting_order.subcontracting_order import (
				update_subcontracting_order_status,
			)

			update_subcontracting_order_status(self.subcontracting_order)

	def update_pick_list_status(self):
		from erpnext.stock.doctype.pick_list.pick_list import update_pick_list_status

		update_pick_list_status(self.pick_list)

	def set_missing_values(self):
		"Updates rate and availability of all the items of mapped doc."
		self.set_transfer_qty()
		self.set_actual_qty()
		self.calculate_rate_and_amount()


@frappe.whitelist()
def make_stock_in_entry(source_name: str, target_doc: str | Document | None = None):
	def set_missing_values(source, target):
		target.stock_entry_type = "Material Transfer"
		target.set_missing_values()

		if not frappe.get_single_value("Stock Settings", "use_serial_batch_fields"):
			target.make_serial_and_batch_bundle_for_transfer()

	def update_item(source_doc, target_doc, source_parent):
		target_doc.t_warehouse = ""

		if source_doc.material_request_item and source_doc.material_request:
			add_to_transit = frappe.db.get_value("Stock Entry", source_name, "add_to_transit")
			if add_to_transit:
				warehouse = frappe.get_value(
					"Material Request Item", source_doc.material_request_item, "warehouse"
				)
				target_doc.t_warehouse = warehouse

		target_doc.s_warehouse = source_doc.t_warehouse
		target_doc.qty = source_doc.qty - source_doc.transferred_qty

	doclist = get_mapped_doc(
		"Stock Entry",
		source_name,
		{
			"Stock Entry": {
				"doctype": "Stock Entry",
				"field_map": {"name": "outgoing_stock_entry"},
				"validation": {"docstatus": ["=", 1]},
			},
			"Stock Entry Detail": {
				"doctype": "Stock Entry Detail",
				"field_map": {
					"name": "ste_detail",
					"parent": "against_stock_entry",
					"serial_no": "serial_no",
					"batch_no": "batch_no",
				},
				"postprocess": update_item,
				"condition": lambda doc: flt(doc.qty) - flt(doc.transferred_qty) > 0.00001,
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist


@frappe.whitelist()
def get_work_order_details(work_order: str, company: str):
	work_order = frappe.get_doc("Work Order", work_order)
	pending_qty_to_produce = flt(work_order.qty) - flt(work_order.produced_qty)

	return {
		"from_bom": 1,
		"bom_no": work_order.bom_no,
		"use_multi_level_bom": work_order.use_multi_level_bom,
		"wip_warehouse": work_order.wip_warehouse,
		"fg_warehouse": work_order.fg_warehouse,
		"fg_completed_qty": pending_qty_to_produce,
	}


def get_consumed_operating_cost(wo_name, bom_no):
	table = frappe.qb.DocType("Stock Entry")
	child_table = frappe.qb.DocType("Landed Cost Taxes and Charges")
	query = (
		frappe.qb.from_(child_table)
		.join(table)
		.on(child_table.parent == table.name)
		.select(Sum(child_table.amount).as_("consumed_cost"))
		.where(
			(table.docstatus == 1)
			& (table.work_order == wo_name)
			& (table.purpose == "Manufacture")
			& (table.bom_no == bom_no)
			& (child_table.has_operating_cost == 1)
		)
	)
	cost = query.run(pluck="consumed_cost")
	return cost[0] if cost and cost[0] else 0


def get_operating_cost_per_unit(work_order=None, bom_no=None):
	operating_cost_per_unit = 0
	if work_order:
		if (
			bom_no
			and frappe.db.get_single_value(
				"Manufacturing Settings", "set_op_cost_and_secondary_items_from_sub_assemblies"
			)
			and frappe.get_cached_value("Work Order", work_order.name, "use_multi_level_bom")
		):
			return get_op_cost_from_sub_assemblies(bom_no)

		if not bom_no:
			bom_no = work_order.bom_no

		for d in work_order.get("operations"):
			if flt(d.completed_qty):
				if not (remaining_qty := flt(d.completed_qty - work_order.produced_qty)):
					continue
				operating_cost_per_unit += (
					flt(d.actual_operating_cost - get_consumed_operating_cost(work_order.name, bom_no))
					/ remaining_qty
				)
			elif work_order.qty:
				operating_cost_per_unit += flt(d.planned_operating_cost) / flt(work_order.qty)

	# Get operating cost from BOM if not found in work_order.
	if not operating_cost_per_unit and bom_no:
		bom = frappe.db.get_value("BOM", bom_no, ["operating_cost", "quantity"], as_dict=1)
		if bom.quantity:
			operating_cost_per_unit = flt(bom.operating_cost) / flt(bom.quantity)

	return operating_cost_per_unit


def get_used_alternative_items(
	subcontract_order=None, subcontract_order_field="subcontracting_order", work_order=None
):
	cond = ""

	if subcontract_order:
		cond = f"and ste.purpose = 'Send to Subcontractor' and ste.{subcontract_order_field} = '{subcontract_order}'"
	elif work_order:
		cond = f"and ste.purpose = 'Material Transfer for Manufacture' and ste.work_order = '{work_order}'"

	if not cond:
		return {}

	used_alternative_items = {}
	data = frappe.db.sql(
		f""" select sted.original_item, sted.uom, sted.conversion_factor,
			sted.item_code, sted.item_name, sted.conversion_factor,sted.stock_uom, sted.description
		from
			`tabStock Entry` ste, `tabStock Entry Detail` sted
		where
			sted.parent = ste.name and ste.docstatus = 1 and sted.original_item !=  sted.item_code
			{cond} """,
		as_dict=1,
	)

	for d in data:
		used_alternative_items[d.original_item] = d

	return used_alternative_items


@frappe.whitelist()
def get_uom_details(item_code: str, uom: str, qty: float | None):
	"""Returns dict `{"conversion_factor": [value], "transfer_qty": qty * [value]}`
	:param args: dict with `item_code`, `uom` and `qty`"""
	conversion_factor = get_conversion_factor(item_code, uom).get("conversion_factor")

	if not conversion_factor:
		frappe.msgprint(_("UOM conversion factor required for UOM: {0} in Item: {1}").format(uom, item_code))
		ret = {"uom": ""}
	else:
		ret = {
			"conversion_factor": flt(conversion_factor),
			"transfer_qty": flt(qty) * flt(conversion_factor),
		}
	return ret


@frappe.whitelist()
def get_warehouse_details(args: str | dict):
	if isinstance(args, str):
		args = json.loads(args)

	args = frappe._dict(args)

	ret = {}
	if args.warehouse and args.item_code:
		args.update(
			{
				"posting_date": args.posting_date,
				"posting_time": args.posting_time,
			}
		)
		ret = {
			"actual_qty": get_previous_sle(args).get("qty_after_transaction") or 0,
			"basic_rate": get_incoming_rate(args),
		}
	return ret


def get_supplied_items(
	subcontract_order, rm_detail_field="sco_rm_detail", subcontract_order_field="subcontracting_order"
):
	fields = [
		"`tabStock Entry Detail`.`transfer_qty`",
		"`tabStock Entry`.`is_return`",
		f"`tabStock Entry Detail`.`{rm_detail_field}`",
		"`tabStock Entry Detail`.`item_code`",
	]

	filters = [
		["Stock Entry", "docstatus", "=", 1],
		["Stock Entry", subcontract_order_field, "=", subcontract_order],
	]

	supplied_item_details = {}
	for row in frappe.get_all("Stock Entry", fields=fields, filters=filters):
		if not row.get(rm_detail_field):
			continue

		key = row.get(rm_detail_field)
		if key not in supplied_item_details:
			supplied_item_details.setdefault(
				key, frappe._dict({"supplied_qty": 0, "returned_qty": 0, "total_supplied_qty": 0})
			)

		supplied_item = supplied_item_details[key]

		if row.is_return:
			supplied_item.returned_qty += row.transfer_qty
		else:
			supplied_item.supplied_qty += row.transfer_qty

		supplied_item.total_supplied_qty = flt(supplied_item.supplied_qty) - flt(supplied_item.returned_qty)

	return supplied_item_details


@frappe.whitelist()
def get_items_from_subcontract_order(source_name: str, target_doc: str | Document | None = None):
	from erpnext.controllers.subcontracting_controller import make_rm_stock_entry

	if isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	order_doctype = "Purchase Order" if target_doc.purchase_order else "Subcontracting Order"
	target_doc = make_rm_stock_entry(
		subcontract_order=source_name, order_doctype=order_doctype, target_doc=target_doc
	)

	return target_doc
