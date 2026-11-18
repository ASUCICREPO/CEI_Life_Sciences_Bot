"""Screen 2: admin email and the allowed sign-up email domains."""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Static

from ...config import EMAIL_PATTERN
from .base import WizardScreen


def _domain_of(email: str) -> str:
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].strip()


class BasicsScreen(WizardScreen):
    def compose(self):
        yield Static("Basics", id="title")
        with VerticalScroll(id="basics-body"):
            yield Static("Admin alert email address:")
            yield Input(value=self.wizard.cfg.adminEmail, placeholder="you@example.com", id="admin-email")
            yield Static("Allowed sign-up email domains (comma-separated):")
            yield Input(
                value=", ".join(self.wizard.cfg.allowedEmailDomains), placeholder="example.com", id="email-domains",
            )
            yield Static("", id="error-message")
        yield self.footer_buttons()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "admin-email":
            domains_input = self.query_one("#email-domains", Input)
            if self.wizard.email_domain_auto:
                domain = _domain_of(event.value)
                if domain:
                    domains_input.value = domain
        elif event.input.id == "email-domains":
            # Once the user edits this field directly, stop auto-filling it from the email.
            domains_input = self.query_one("#email-domains", Input)
            email_input = self.query_one("#admin-email", Input)
            if domains_input.value.strip() != _domain_of(email_input.value):
                self.wizard.email_domain_auto = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nav-back":
            self.action_go_back()
            return
        if event.button.id != "nav-next":
            return

        admin_email = self.query_one("#admin-email", Input).value.strip()
        domains_raw = self.query_one("#email-domains", Input).value
        domains = [d.strip() for d in domains_raw.split(",") if d.strip()]

        if not admin_email or not EMAIL_PATTERN.match(admin_email):
            self.set_error("Enter a valid admin email address.")
            return
        if not domains:
            self.set_error("Enter at least one allowed sign-up email domain.")
            return

        self.wizard.cfg.adminEmail = admin_email
        self.wizard.cfg.allowedEmailDomains = domains
        self.set_error("")

        from .models import ModelsScreen

        self.app.push_screen(ModelsScreen())
