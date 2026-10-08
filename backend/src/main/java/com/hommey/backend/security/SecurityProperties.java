package com.hommey.backend.security;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.core.io.Resource;

@ConfigurationProperties("hommey.security")
public record SecurityProperties(
    String issuer, Resource publicKey, Resource privateKey, Duration agentDuration) {}
