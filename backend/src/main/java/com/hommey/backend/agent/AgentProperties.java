package com.hommey.backend.agent;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties("hommey.agent")
public record AgentProperties(String baseUrl) {}
