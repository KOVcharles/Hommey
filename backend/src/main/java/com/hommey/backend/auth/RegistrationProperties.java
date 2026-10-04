package com.hommey.backend.auth;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties("hommey.registration")
public record RegistrationProperties(
    String altchaSecret,
    String resendApiKey,
    String resendFrom,
    String resendUrl,
    String adminEmails) {}
