package com.hommey.backend.web;

import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.*;

@Controller
public class PageController {
  @GetMapping("/")
  public String index() {
    return "login";
  }

  @GetMapping("/login")
  public String login() {
    return "signin";
  }

  @GetMapping("/signup")
  public String signup() {
    return "signup";
  }

  @GetMapping("/chat/{user}")
  public String chat(@PathVariable String user, Model model) {
    model.addAttribute("userId", user);
    return "chat";
  }

  @GetMapping("/admin/skills")
  public String skills() {
    return "admin_skills";
  }
}
