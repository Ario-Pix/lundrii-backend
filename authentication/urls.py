"""Authentication API routes."""

from django.urls import path

from authentication import views

urlpatterns = [
    path("signup-options", views.SignupOptionsView.as_view(), name="auth-signup-options"),
    path("register", views.RegisterView.as_view(), name="auth-register"),
    path("login", views.LoginView.as_view(), name="auth-login"),
    path("login/request-otp", views.LoginRequestOtpView.as_view(), name="auth-login-request-otp"),
    path("login/verify-otp", views.LoginVerifyOtpView.as_view(), name="auth-login-verify-otp"),
    path("logout", views.LogoutView.as_view(), name="auth-logout"),
    path("refresh", views.RefreshView.as_view(), name="auth-refresh"),
    path("verify-email", views.VerifyEmailView.as_view(), name="auth-verify-email"),
    path("resend-verification", views.ResendVerificationView.as_view(), name="auth-resend-verification"),
    path("forgot-password", views.ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("reset-password", views.ResetPasswordView.as_view(), name="auth-reset-password"),
]
