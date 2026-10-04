package com.hommey.backend.security;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.hommey.backend.common.ApiErrors;
import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.RSAKey;
import com.nimbusds.jose.jwk.source.ImmutableJWKSet;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.interfaces.RSAPrivateKey;
import java.security.interfaces.RSAPublicKey;
import java.security.spec.PKCS8EncodedKeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.util.Base64;
import java.util.List;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.authorization.AuthorizationDecision;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.oauth2.core.*;
import org.springframework.security.oauth2.jwt.*;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
public class SecurityConfiguration {
  @Bean
  RSAPublicKey publicKey(SecurityProperties props) throws Exception {
    return (RSAPublicKey)
        KeyFactory.getInstance("RSA")
            .generatePublic(new X509EncodedKeySpec(pem(props.publicKey())));
  }

  @Bean
  JwtEncoder encoder(SecurityProperties props, RSAPublicKey pub) throws Exception {
    var priv =
        (RSAPrivateKey)
            KeyFactory.getInstance("RSA")
                .generatePrivate(new PKCS8EncodedKeySpec(pem(props.privateKey())));
    return new NimbusJwtEncoder(
        new ImmutableJWKSet<>(
            new JWKSet(new RSAKey.Builder(pub).privateKey(priv).keyID("hommey").build())));
  }

  private static byte[] pem(org.springframework.core.io.Resource resource) throws Exception {
    try (var input = resource.getInputStream()) {
      String s = new String(input.readAllBytes(), StandardCharsets.US_ASCII);
      return Base64.getDecoder().decode(s.replaceAll("-----[^-]+-----", "").replaceAll("\\s", ""));
    }
  }

  @Bean
  JwtDecoder decoder(SecurityProperties props, RSAPublicKey pub) {
    var decoder = NimbusJwtDecoder.withPublicKey(pub).build();
    OAuth2TokenValidator<Jwt> audience =
        jwt ->
            jwt.getAudience().stream().anyMatch(List.of("hommey-api", "hommey-business")::contains)
                ? OAuth2TokenValidatorResult.success()
                : OAuth2TokenValidatorResult.failure(new OAuth2Error("invalid_token"));
    decoder.setJwtValidator(
        new DelegatingOAuth2TokenValidator<>(
            JwtValidators.createDefaultWithIssuer(props.issuer()), audience));
    return decoder;
  }

  @Bean
  PasswordEncoder passwordEncoder() {
    return new BCryptPasswordEncoder(12);
  }

  @Bean
  SecurityFilterChain security(HttpSecurity http, ObjectMapper mapper) throws Exception {
    return http.csrf(csrf -> csrf.disable())
        .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
        .authorizeHttpRequests(
            a ->
                a.requestMatchers(
                        "/auth/**",
                        "/actuator/health/**",
                        "/",
                        "/login",
                        "/signup",
                        "/chat/**",
                        "/admin/skills",
                        "/static/**")
                    .permitAll()
                    .requestMatchers("/internal/business/**")
                    .access(
                        (authentication, context) ->
                            new AuthorizationDecision(
                                authentication.get().getPrincipal() instanceof Jwt jwt
                                    && "agent".equals(jwt.getClaimAsString("type"))
                                    && jwt.getAudience().contains("hommey-business")))
                    .requestMatchers("/api/**")
                    .access(
                        (authentication, context) ->
                            new AuthorizationDecision(
                                authentication.get().getPrincipal() instanceof Jwt jwt
                                    && "access".equals(jwt.getClaimAsString("type"))
                                    && jwt.getAudience().contains("hommey-api")))
                    .anyRequest()
                    .denyAll())
        .oauth2ResourceServer(
            o ->
                o.jwt(j -> {})
                    .authenticationEntryPoint(
                        (req, res, ex) ->
                            ApiErrors.write(mapper, req, res, 401, "UNAUTHORIZED", "请登录后重试")))
        .exceptionHandling(
            e ->
                e.accessDeniedHandler(
                    (req, res, ex) ->
                        ApiErrors.write(mapper, req, res, 403, "FORBIDDEN", "无权访问此资源")))
        .build();
  }
}
