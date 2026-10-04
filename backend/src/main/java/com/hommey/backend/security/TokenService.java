package com.hommey.backend.security;

import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.security.oauth2.jose.jws.SignatureAlgorithm;
import org.springframework.security.oauth2.jwt.*;
import org.springframework.stereotype.Service;

@Service
public class TokenService {
  private final JwtEncoder encoder;
  private final JwtDecoder decoder;
  private final SecurityProperties props;

  public TokenService(JwtEncoder encoder, JwtDecoder decoder, SecurityProperties props) {
    this.encoder = encoder;
    this.decoder = decoder;
    this.props = props;
  }

  public String issue(String user, String type) {
    return sign(
        JwtClaimsSet.builder()
            .issuer(props.issuer())
            .subject(user)
            .audience(List.of("hommey-api"))
            .issuedAt(Instant.now())
            .expiresAt(
                Instant.now()
                    .plus(
                        "refresh".equals(type) ? props.refreshDuration() : props.accessDuration()))
            .id(UUID.randomUUID().toString())
            .claim("type", type)
            .build());
  }

  public String agent(String user, String role, String session, String request) {
    var builder =
        JwtClaimsSet.builder()
            .issuer(props.issuer())
            .subject(user)
            .audience(List.of("hommey-agent", "hommey-business"))
            .issuedAt(Instant.now())
            .expiresAt(Instant.now().plus(props.agentDuration()))
            .id(UUID.randomUUID().toString())
            .claim("type", "agent")
            .claim("role", role);
    if (session != null) builder.claim("session_id", session);
    if (request != null) builder.claim("request_id", request);
    return sign(builder.build());
  }

  private String sign(JwtClaimsSet claims) {
    return encoder
        .encode(
            JwtEncoderParameters.from(
                JwsHeader.with(SignatureAlgorithm.RS256).keyId("hommey").build(), claims))
        .getTokenValue();
  }

  public Jwt refresh(String token) {
    try {
      var jwt = decoder.decode(token);
      if (!"refresh".equals(jwt.getClaimAsString("type")))
        throw new JwtException("Wrong token type");
      return jwt;
    } catch (JwtException ex) {
      throw new com.hommey.backend.common.ApiException(401, "UNAUTHORIZED", "刷新令牌无效或已过期");
    }
  }
}
