variable "email_alertas" {
  description = "E-mail inscrito no tópico de alertas. Fica em terraform.tfvars, fora do git."
  type        = string
  sensitive   = true
}
