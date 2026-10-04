package com.hommey.backend.auth;

import com.hommey.backend.security.AccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

@RestController
public class AuthController {
  public record Login(
      @NotBlank @Email @Size(max = 254) String email, @NotBlank @Size(max = 72) String password) {}

  public record Register(
      @NotBlank @Email @Size(max = 254) String email,
      @NotBlank @Size(min = 8, max = 72) String password,
      @NotBlank @Pattern(regexp = "[0-9]{6}") String code,
      @NotBlank @Size(max = 128) String inviteCode) {}

  public record SendCode(
      @NotBlank @Email @Size(max = 254) String email,
      @NotBlank @Size(max = 128) String inviteCode,
      @NotBlank @Size(max = 10000) String altcha) {}

  public record Refresh(@NotBlank @Size(max = 8192) String refreshToken) {}

  private final AuthService auth;
  private final RegistrationService registration;
  private final AccessPolicy access;

  public AuthController(AuthService auth, RegistrationService registration, AccessPolicy access) {
    this.auth = auth;
    this.registration = registration;
    this.access = access;
  }

  @PostMapping("/auth/login")
  public Object login(@Valid @RequestBody Login body) {
    return auth.login(body);
  }

  @PostMapping("/auth/refresh")
  public Object refresh(@Valid @RequestBody Refresh body) {
    return auth.refresh(body.refreshToken());
  }

  @GetMapping("/auth/altcha-challenge")
  public Object challenge() {
    return registration.challenge();
  }

  @PostMapping("/auth/send-verification-code")
  public Object send(@Valid @RequestBody SendCode body, HttpServletRequest request) {
    registration.send(body, request.getRemoteAddr());
    return Map.of("sent", true);
  }

  @PostMapping("/auth/register")
  @ResponseStatus(HttpStatus.CREATED)
  public Object register(@Valid @RequestBody Register body) {
    return auth.register(body);
  }

  @GetMapping("/api/me")
  public Object me(@AuthenticationPrincipal Jwt jwt) {
    var user = access.requireUser(jwt, jwt.getSubject());
    return Map.of("id", user.id(), "email", user.email(), "role", user.role());
  }
}
